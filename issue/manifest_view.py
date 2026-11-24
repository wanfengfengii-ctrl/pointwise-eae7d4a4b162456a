import json
from dataclasses import dataclass

from source.locations import RevisionRef, require_name


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
