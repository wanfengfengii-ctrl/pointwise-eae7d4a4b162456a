import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from issue.manifest_view import FrozenManifest
from receipt.inventory import AcceptedPiece, read_accepted


@dataclass(frozen=True, slots=True)
class SelectedSegment:
    accepted: AcceptedPiece

    @property
    def revision(self):
        return self.accepted.verified.envelope.identity.revision


def select_segments(
    inventory: sqlite3.Connection, manifest: FrozenManifest, accepted_ids: Iterable[int]
) -> tuple[SelectedSegment, ...]:
    slots = {slot.segment: slot for slot in manifest.slots}
    selected = {}
    for accepted_id in accepted_ids:
        try:
            piece = read_accepted(inventory, accepted_id)
        except LookupError as exc:
            raise ValueError(f'cannot select receipt {accepted_id!r}') from exc
        identity = piece.verified.envelope.identity
        ref = identity.revision
        if identity.manifest_id != manifest.manifest_id or ref.roll != manifest.roll:
            raise ValueError(f'receipt {accepted_id} belongs to another frozen manifest or roll')
        slot = slots.get(ref.segment)
        if slot is None or ref not in slot.candidates:
            raise ValueError(f'receipt {accepted_id} is outside the permitted slots or revisions')
        if ref.segment in selected:
            raise ValueError(f'more than one receipt selected for {ref.segment!r}')
        selected[ref.segment] = SelectedSegment(piece)
    missing = tuple(slot.segment for slot in manifest.slots if slot.segment not in selected)
    if missing:
        raise ValueError(f'missing selected segments: {missing!r}')
    return tuple(selected[slot.segment] for slot in manifest.slots)
