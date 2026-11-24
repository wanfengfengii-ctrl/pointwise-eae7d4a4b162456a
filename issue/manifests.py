import sqlite3
from collections.abc import Iterable
from uuid import uuid4

from issue.eligibility import validate_candidates
from issue.manifest_view import FrozenManifest, ManifestSlot
from source.locations import require_name


def open_manifest_store(path) -> sqlite3.Connection:
    store = sqlite3.connect(path, isolation_level=None)
    try:
        store.execute('PRAGMA foreign_keys = ON')
        store.execute('''CREATE TABLE IF NOT EXISTS manifests (
            manifest_id TEXT PRIMARY KEY NOT NULL,
            payload BLOB NOT NULL
        )''')
    except BaseException:
        store.close()
        raise
    return store


def freeze(
    source: sqlite3.Connection, issuer: sqlite3.Connection, roll: str, slots: Iterable[ManifestSlot]
) -> FrozenManifest:
    manifest = FrozenManifest(uuid4().hex, roll, tuple(slots))
    validate_candidates(source, manifest)
    issuer.execute('BEGIN IMMEDIATE')
    with issuer:
        issuer.execute(
            'INSERT INTO manifests (manifest_id, payload) VALUES (?, ?)',
            (manifest.manifest_id, manifest.to_bytes()),
        )
    return manifest


def read_manifest(issuer: sqlite3.Connection, manifest_id: str) -> FrozenManifest:
    require_name(manifest_id)
    row = issuer.execute('SELECT payload FROM manifests WHERE manifest_id = ?', (manifest_id,)).fetchone()
    if row is None:
        raise LookupError(f'unknown manifest {manifest_id!r}')
    return FrozenManifest.from_bytes(row[0])
