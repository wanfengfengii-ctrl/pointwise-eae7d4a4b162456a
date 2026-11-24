"""Selection and original-roll framing for windowed (V2) pieces."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from assembly.origins import ValueOrigin
from assembly.totals import TalliedRecord
from issue.manifest_view import WindowedManifest
from issue.wire_v2 import (
    INCOMING_ESCAPE,
    V2BoundaryBody,
    V2Envelope,
    V2WindowBody,
)
from receipt.inventory import read_accepted
from reports.snapshot_store import ReportSelection
from source.locations import ByteLocation


@dataclass(frozen=True, slots=True)
class ChosenBoundary:
    accepted_id: int
    piece_id: str
    envelope: V2Envelope
    body: V2BoundaryBody


@dataclass(frozen=True, slots=True)
class ChosenWindow:
    accepted_id: int
    piece_id: str
    envelope: V2Envelope
    body: V2WindowBody


@dataclass(frozen=True, slots=True)
class ChosenSlot:
    segment: str
    boundary: ChosenBoundary
    windows: tuple[ChosenWindow, ...]


@dataclass(frozen=True, slots=True)
class WindowedChoice:
    slots: tuple[ChosenSlot, ...]
    selections: tuple[ReportSelection, ...]


def _dedupe(accepted_ids: Iterable[int]) -> list[int]:
    unique: list[int] = []
    seen: set = set()
    for accepted_id in accepted_ids:
        try:
            if accepted_id in seen:
                continue
            seen.add(accepted_id)
        except TypeError:
            if any(accepted_id == other for other in unique):
                continue
        unique.append(accepted_id)
    return unique


def select_windowed(
    inventory: sqlite3.Connection, manifest: WindowedManifest, accepted_ids: Iterable[int]
) -> WindowedChoice:
    if not isinstance(manifest, WindowedManifest):
        raise ValueError('close_windowed requires a restored V2 windowed manifest')
    slots_by_name = {slot.segment: slot for slot in manifest.slots}
    boundaries: dict[str, list[ChosenBoundary]] = {}
    windows: dict[str, list[ChosenWindow]] = {}
    for accepted_id in _dedupe(accepted_ids):
        try:
            piece = read_accepted(inventory, accepted_id)
        except LookupError as exc:
            raise ValueError(f'cannot select receipt {accepted_id!r}') from exc
        envelope = piece.verified.envelope
        if not isinstance(envelope, V2Envelope):
            raise ValueError(f'receipt {accepted_id} is not a V2 windowed piece')
        identity = envelope.identity
        ref = identity.revision
        if identity.manifest_id != manifest.manifest_id or ref.roll != manifest.roll:
            raise ValueError(f'receipt {accepted_id} belongs to another frozen manifest or roll')
        slot = slots_by_name.get(ref.segment)
        if slot is None or ref not in slot.candidates:
            raise ValueError(f'receipt {accepted_id} is outside the permitted slots or revisions')
        if isinstance(envelope.body, V2BoundaryBody):
            boundaries.setdefault(ref.segment, []).append(
                ChosenBoundary(piece.accepted_id, piece.verified.piece_id, envelope, envelope.body)
            )
        elif isinstance(envelope.body, V2WindowBody):
            windows.setdefault(ref.segment, []).append(
                ChosenWindow(piece.accepted_id, piece.verified.piece_id, envelope, envelope.body)
            )
        else:  # pragma: no cover - decode guarantees one of the two bodies
            raise ValueError(f'receipt {accepted_id} has an invalid V2 body')

    chosen_slots: list[ChosenSlot] = []
    selections: list[ReportSelection] = []
    for slot in manifest.slots:
        segment = slot.segment
        slot_boundaries = boundaries.get(segment, ())
        if len(slot_boundaries) != 1:
            raise ValueError(f'segment {segment!r} requires exactly one boundary piece')
        boundary = slot_boundaries[0]
        ref = boundary.envelope.identity.revision
        binding = manifest.windowing_for(ref)
        if binding is None:
            raise ValueError(f'boundary {boundary.piece_id} is not bound to the frozen windowing')
        if (boundary.body.body_start, boundary.body.body_end) != (
            binding.body_start,
            binding.body_end,
        ):
            raise ValueError(f'boundary {boundary.piece_id} has a mismatched body range')
        frozen_windows = tuple((w.name, w.start, w.end) for w in binding.windows)
        if boundary.body.windows != frozen_windows:
            raise ValueError(f'boundary {boundary.piece_id} has mismatched permitted windows')

        slot_windows = windows.get(segment, ())
        chosen_windows: list[ChosenWindow] = []
        seen_names: set[str] = set()
        for window in slot_windows:
            window_ref = window.envelope.identity.revision
            if window_ref != ref:
                raise ValueError(
                    f'window {window.piece_id} is bound to a different revision than its boundary'
                )
            name = window.body.window_name
            if name in seen_names:
                raise ValueError(f'multiple selections for window {name!r} of {segment!r}')
            seen_names.add(name)
            frozen = binding.window(name)
            if frozen is None or (window.body.start, window.body.end) != (frozen.start, frozen.end):
                raise ValueError(f'window {window.piece_id} is wrongly bound to the freeze')
            chosen_windows.append(window)
        chosen_windows.sort(key=lambda item: item.body.window_name)

        chosen_slots.append(ChosenSlot(segment, boundary, tuple(chosen_windows)))
        selections.append(
            ReportSelection(boundary.accepted_id, boundary.piece_id, ref)
        )
        for window in chosen_windows:
            selections.append(
                ReportSelection(
                    window.accepted_id,
                    window.piece_id,
                    window.envelope.identity.revision,
                )
            )
    return WindowedChoice(tuple(chosen_slots), tuple(selections))


@dataclass(frozen=True, slots=True)
class _SegmentLayout:
    chosen: ChosenSlot
    global_start: int
    body_start: int
    body_end: int

    @property
    def length(self) -> int:
        return self.body_end - self.body_start

    @property
    def ref(self):
        return self.chosen.boundary.envelope.identity.revision

    def checkpoint(self, coord: int, entry_escaped: bool):
        for point in self.chosen.boundary.body.checkpoints:
            if point.coord == coord:
                return point.escaped if entry_escaped else point.normal
        raise ValueError(f'boundary for {self.chosen.segment!r} lacks framing checkpoint at {coord}')


def _build_coverage(manifest: WindowedManifest, choice: WindowedChoice):
    layouts: list[_SegmentLayout] = []
    covered: dict[int, int] = {}
    origins: dict[int, ByteLocation] = {}
    global_start = 0
    for chosen in choice.slots:
        ref = chosen.boundary.envelope.identity.revision
        binding = manifest.windowing_for(ref)
        body_start = chosen.boundary.body.body_start
        body_end = chosen.boundary.body.body_end
        layout = _SegmentLayout(chosen, global_start, body_start, body_end)
        layouts.append(layout)
        for window in chosen.windows:
            body = window.body
            offset0 = global_start + (body.start - body_start)
            for index, octet in enumerate(body.data):
                global_coord = offset0 + index
                raw_offset = body.start + index
                if global_coord in covered and covered[global_coord] != octet:
                    raise ValueError('overlapping windows contain contradictory bytes')
                covered[global_coord] = octet
                origins[global_coord] = ByteLocation(ref, raw_offset)
        global_start += layout.length
    return tuple(layouts), covered, origins


def _layout_for(layouts: tuple[_SegmentLayout, ...], global_coord: int) -> _SegmentLayout:
    for layout in layouts:
        if layout.global_start <= global_coord < layout.global_start + layout.length:
            return layout
    raise ValueError('cannot locate covered byte in the frozen segment order')


def assemble_windowed_records(
    manifest: WindowedManifest, choice: WindowedChoice
) -> tuple[TalliedRecord, ...]:
    layouts, covered, origins = _build_coverage(manifest, choice)

    # Chain the full-body framing state across segments using boundary summaries.
    entry_escaped = False
    prior_ends = 0
    incoming_escape: ByteLocation | None = None
    carried_has_readings = False
    state_by_segment: dict[str, tuple[bool, int, ByteLocation | None, bool]] = {}
    last_end_triple = None
    for layout in layouts:
        state_by_segment[layout.chosen.segment] = (
            entry_escaped,
            prior_ends,
            incoming_escape,
            carried_has_readings,
        )
        end_triple = layout.checkpoint(layout.body_end, entry_escaped)
        ends, open_count, pending = end_triple
        if pending is None:
            incoming_escape = None
        elif pending == INCOMING_ESCAPE:
            if incoming_escape is None:
                raise ValueError('boundary reports an incoming escape without a carried location')
        else:
            incoming_escape = ByteLocation(layout.ref, pending)
        # Once an in-segment terminator fires it resets the open record, so the
        # end-state readings are purely local; with no terminator the open record
        # may be the one carried into this segment.
        if ends > 0:
            carried_has_readings = open_count > 0
        else:
            carried_has_readings = carried_has_readings or open_count > 0
        prior_ends += ends
        entry_escaped = pending is not None
        last_end_triple = end_triple

    if last_end_triple is not None:
        _ends, _open_count, pending = last_end_triple
        if pending is not None:
            raise ValueError('recording ends with a dangling escape')
        if carried_has_readings:
            raise ValueError('recording ends with an unfinished record')

    # Maximal globally-contiguous covered runs (merge across segment seams).
    records: list[TalliedRecord] = []
    coordinates = sorted(covered)
    runs: list[tuple[int, int]] = []
    for coord in coordinates:
        if not runs or coord != runs[-1][1]:
            runs.append((coord, coord + 1))
        else:
            start, _end = runs[-1]
            runs[-1] = (start, coord + 1)

    for run_start, run_end in runs:
        layout = _layout_for(layouts, run_start)
        (
            entry_for_layout,
            segment_prior_ends,
            entry_escape_loc,
            entry_has_readings,
        ) = state_by_segment[layout.chosen.segment]
        local_coord = layout.body_start + (run_start - layout.global_start)
        ends_at_start, open_at_start, pending_at_start = layout.checkpoint(
            local_coord, entry_for_layout
        )
        base_ends = segment_prior_ends + ends_at_start

        if pending_at_start is None:
            pending_loc: ByteLocation | None = None
        elif pending_at_start == INCOMING_ESCAPE:
            pending_loc = entry_escape_loc
            if pending_loc is None:
                raise ValueError('cannot bind an incoming escape at a window boundary')
        else:
            pending_loc = ByteLocation(layout.ref, pending_at_start)

        # The run starts a fresh record only when no escape is pending and the
        # current record has no readings before this coordinate. If the prefix
        # has a terminator, the open count is local after the last one; with no
        # terminator the record (and its readings) may be carried from before.
        if pending_at_start is not None:
            signable = False
        elif ends_at_start > 0:
            signable = open_at_start == 0
        else:
            signable = open_at_start == 0 and not entry_has_readings
        ends_seen = 0
        values: list[int] = []
        value_origins: list[ValueOrigin] = []
        for global_coord in range(run_start, run_end):
            octet = covered[global_coord]
            data_location = origins[global_coord]
            if pending_loc is not None:
                values.append(octet)
                value_origins.append(ValueOrigin((pending_loc, data_location)))
                pending_loc = None
            elif octet == 0x1B:
                pending_loc = data_location
            elif octet == 0x0A:
                ends_seen += 1
                record_number = base_ends + ends_seen
                if signable:
                    records.append(
                        TalliedRecord(
                            record_number,
                            tuple(values),
                            sum(values),
                            tuple(value_origins),
                        )
                    )
                values = []
                value_origins = []
                signable = True
            else:
                values.append(octet)
                value_origins.append(ValueOrigin((data_location,)))

    records.sort(key=lambda record: record.number)
    numbers = [record.number for record in records]
    if len(set(numbers)) != len(numbers):
        raise ValueError('framing summaries yield contradictory record numbering')
    return tuple(records)
