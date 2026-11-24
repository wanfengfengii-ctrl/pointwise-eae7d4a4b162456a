import sqlite3

from issue.manifest_view import FrozenManifest
from source.locations import RevisionRef
from source.revisions import read_revision


def validate_candidates(source: sqlite3.Connection, manifest: FrozenManifest) -> None:
    for slot in manifest.slots:
        for ref in slot.candidates:
            if ref.roll != manifest.roll:
                raise ValueError(f'candidate belongs to another roll: {ref!r}')
            try:
                read_revision(source, ref)
            except LookupError as exc:
                raise ValueError(f'candidate revision does not exist: {ref!r}') from exc


def require_issuable_revision(manifest: FrozenManifest, segment: str, revision: int) -> RevisionRef:
    ref = RevisionRef(manifest.roll, segment, revision)
    slot = manifest.slot_for(segment)
    if slot is None or ref not in slot.candidates:
        raise ValueError(f'revision is outside the frozen candidates: {ref!r}')
    return ref
