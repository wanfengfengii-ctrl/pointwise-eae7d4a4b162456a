import json
from dataclasses import dataclass

from source.locations import RevisionRef, require_name


WINDOWED_MANIFEST_KIND = 'windowed-manifest'


@dataclass(frozen=True, slots=True)
class ManifestSlot:
    segment: str
    candidates: tuple[RevisionRef, ...]

    def __post_init__(self) -> None:
        require_name(self.segment)
        if type(self.candidates) is not tuple or not self.candidates:
            raise ValueError('slot requires a nonempty tuple of revisions')
        if any(not isinstance(ref, RevisionRef) or ref.segment != self.segment for ref in self.candidates):
            raise ValueError('candidate does not refer to the slot segment')
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError('duplicate candidate in slot')


@dataclass(frozen=True, slots=True)
class FrozenManifest:
    manifest_id: str
    roll: str
    slots: tuple[ManifestSlot, ...]

    def __post_init__(self) -> None:
        require_name(self.manifest_id)
        require_name(self.roll)
        if type(self.slots) is not tuple or not self.slots:
            raise ValueError('manifest requires at least one slot')
        if any(not isinstance(slot, ManifestSlot) for slot in self.slots):
            raise ValueError('invalid manifest slot')
        if len({slot.segment for slot in self.slots}) != len(self.slots):
            raise ValueError('duplicate segment position')

    def slot_for(self, segment: str) -> ManifestSlot | None:
        return next((slot for slot in self.slots if slot.segment == segment), None)

    def to_bytes(self) -> bytes:
        data = {
            'manifest_id': self.manifest_id,
            'roll': self.roll,
            'slots': [[slot.segment, [ref.to_list() for ref in slot.candidates]] for slot in self.slots],
        }
        return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')

    @classmethod
    def from_bytes(cls, payload: bytes) -> 'FrozenManifest':
        if type(payload) is not bytes:
            raise ValueError('manifest must be bytes')
        try:
            data = json.loads(payload.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('invalid manifest encoding') from exc
        if type(data) is not dict or set(data) != {'manifest_id', 'roll', 'slots'}:
            raise ValueError('invalid manifest fields')
        if type(data['slots']) is not list:
            raise ValueError('invalid manifest slots')
        slots = []
        for item in data['slots']:
            if type(item) is not list or len(item) != 2 or type(item[1]) is not list:
                raise ValueError('invalid manifest slot representation')
            slots.append(ManifestSlot(item[0], tuple(RevisionRef.from_list(ref) for ref in item[1])))
        manifest = cls(data['manifest_id'], data['roll'], tuple(slots))
        if manifest.to_bytes() != payload:
            raise ValueError('noncanonical manifest encoding')
        return manifest


def require_integer_coordinate(value: object) -> int:
    # ``bool`` is a subclass of ``int`` but booleans are not coordinates.
    if type(value) is not int:
        raise ValueError('window coordinates must be integers')
    return value


@dataclass(frozen=True, slots=True)
class WindowCoord:
    name: str
    start: int
    end: int

    def __post_init__(self) -> None:
        require_name(self.name)
        require_integer_coordinate(self.start)
        require_integer_coordinate(self.end)
        if self.start >= self.end:
            raise ValueError('window requires start < end')

    def to_list(self) -> list:
        return [self.name, self.start, self.end]

    @classmethod
    def from_list(cls, data: object) -> 'WindowCoord':
        if type(data) is not list or len(data) != 3:
            raise ValueError('invalid window representation')
        name, start, end = data
        return cls(name, start, end)


@dataclass(frozen=True, slots=True)
class CandidateWindowing:
    """Frozen body range and permitted windows for one candidate revision."""

    revision: RevisionRef
    body_start: int
    body_end: int
    windows: tuple[WindowCoord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.revision, RevisionRef):
            raise ValueError('windowing requires a revision reference')
        require_integer_coordinate(self.body_start)
        require_integer_coordinate(self.body_end)
        if not 0 <= self.body_start <= self.body_end:
            raise ValueError('invalid frozen body range')
        if type(self.windows) is not tuple or any(not isinstance(w, WindowCoord) for w in self.windows):
            raise ValueError('windows must be a tuple of WindowCoord')
        names = tuple(window.name for window in self.windows)
        if len(set(names)) != len(names):
            raise ValueError('duplicate window name for candidate')
        for window in self.windows:
            if not self.body_start <= window.start < window.end <= self.body_end:
                raise ValueError('window lies outside the frozen data body')

    def window(self, name: str) -> WindowCoord | None:
        return next((item for item in self.windows if item.name == name), None)

    def to_row(self) -> list:
        return [
            self.revision.to_list(),
            self.body_start,
            self.body_end,
            [window.to_list() for window in self.windows],
        ]

    @classmethod
    def from_row(cls, data: object) -> 'CandidateWindowing':
        if type(data) is not list or len(data) != 4:
            raise ValueError('invalid candidate windowing representation')
        ref_data, body_start, body_end, windows_data = data
        if type(windows_data) is not list:
            raise ValueError('invalid frozen windows')
        return cls(
            RevisionRef.from_list(ref_data),
            require_integer_coordinate(body_start),
            require_integer_coordinate(body_end),
            tuple(WindowCoord.from_list(item) for item in windows_data),
        )


@dataclass(frozen=True, slots=True)
class WindowedManifest:
    manifest_id: str
    roll: str
    slots: tuple[ManifestSlot, ...]
    windowings: tuple[CandidateWindowing, ...]

    def __post_init__(self) -> None:
        require_name(self.manifest_id)
        require_name(self.roll)
        if type(self.slots) is not tuple or not self.slots:
            raise ValueError('manifest requires at least one slot')
        if any(not isinstance(slot, ManifestSlot) for slot in self.slots):
            raise ValueError('invalid manifest slot')
        if len({slot.segment for slot in self.slots}) != len(self.slots):
            raise ValueError('duplicate segment position')
        if type(self.windowings) is not tuple or any(
            not isinstance(item, CandidateWindowing) for item in self.windowings
        ):
            raise ValueError('invalid candidate windowings')
        expected = tuple(ref for slot in self.slots for ref in slot.candidates)
        bound = tuple(item.revision for item in self.windowings)
        if len(bound) != len(set(bound)) or set(bound) != set(expected) or len(bound) != len(expected):
            raise ValueError('every candidate requires exactly one frozen window table')
        if any(ref.roll != self.roll for ref in bound):
            raise ValueError('windowing belongs to another roll')

    def slot_for(self, segment: str) -> ManifestSlot | None:
        return next((slot for slot in self.slots if slot.segment == segment), None)

    def windowing_for(self, ref: RevisionRef) -> CandidateWindowing | None:
        return next((item for item in self.windowings if item.revision == ref), None)

    def to_bytes(self) -> bytes:
        data = {
            'kind': WINDOWED_MANIFEST_KIND,
            'manifest_id': self.manifest_id,
            'roll': self.roll,
            'slots': [[slot.segment, [ref.to_list() for ref in slot.candidates]] for slot in self.slots],
            'windowings': [item.to_row() for item in self.windowings],
        }
        return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')

    @classmethod
    def from_bytes(cls, payload: bytes) -> 'WindowedManifest':
        if type(payload) is not bytes:
            raise ValueError('manifest must be bytes')
        try:
            data = json.loads(payload.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('invalid manifest encoding') from exc
        if type(data) is not dict or set(data) != {'kind', 'manifest_id', 'roll', 'slots', 'windowings'}:
            raise ValueError('invalid windowed manifest fields')
        if data['kind'] != WINDOWED_MANIFEST_KIND:
            raise ValueError('not a windowed manifest')
        if type(data['slots']) is not list or type(data['windowings']) is not list:
            raise ValueError('invalid windowed manifest structure')
        slots = []
        for item in data['slots']:
            if type(item) is not list or len(item) != 2 or type(item[1]) is not list:
                raise ValueError('invalid manifest slot representation')
            slots.append(ManifestSlot(item[0], tuple(RevisionRef.from_list(ref) for ref in item[1])))
        manifest = cls(
            data['manifest_id'],
            data['roll'],
            tuple(slots),
            tuple(CandidateWindowing.from_row(item) for item in data['windowings']),
        )
        if manifest.to_bytes() != payload:
            raise ValueError('noncanonical manifest encoding')
        return manifest


def manifest_from_bytes(payload: bytes):
    """Restore either a V1 :class:`FrozenManifest` or a :class:`WindowedManifest`."""
    if type(payload) is not bytes:
        raise ValueError('manifest must be bytes')
    try:
        data = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid manifest encoding') from exc
    if type(data) is dict and data.get('kind') == WINDOWED_MANIFEST_KIND:
        return WindowedManifest.from_bytes(payload)
    return FrozenManifest.from_bytes(payload)
