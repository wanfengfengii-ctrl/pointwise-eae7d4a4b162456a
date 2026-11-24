import sqlite3
from collections.abc import Iterable, Mapping
from uuid import uuid4

from issue.eligibility import validate_candidates
from issue.manifest_view import (
    CandidateWindowing,
    FrozenManifest,
    ManifestSlot,
    WindowCoord,
    WindowedManifest,
    manifest_from_bytes,
    require_integer_coordinate,
)
from source.locations import RevisionRef, require_name
from source.raw_store import load_raw


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


def _normalize_window_table(table: object, raw) -> CandidateWindowing:
    if not isinstance(table, Mapping):
        raise ValueError('each candidate requires a mapping of named windows')
    coords = []
    for name in sorted(table):
        bounds = table[name]
        require_name(name)
        if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
            raise ValueError('window coordinates must be an (start, end) pair')
        start = require_integer_coordinate(bounds[0])
        end = require_integer_coordinate(bounds[1])
        coords.append(WindowCoord(name, start, end))
    return CandidateWindowing(
        raw.ref, raw.body_start, raw.body_end, tuple(coords)
    )


def freeze_windowed(
    source: sqlite3.Connection,
    issuer: sqlite3.Connection,
    roll: str,
    slots: Iterable[ManifestSlot],
    windows: Mapping[RevisionRef, Mapping[str, tuple[int, int]]],
) -> WindowedManifest:
    require_name(roll)
    if not isinstance(windows, Mapping):
        raise ValueError('windows must be keyed by RevisionRef')
    slot_tuple = tuple(slots)
    refs = tuple(ref for slot in slot_tuple for ref in slot.candidates)
    if any(not isinstance(ref, RevisionRef) for ref in refs):
        raise ValueError('slots must contain RevisionRef candidates')
    if set(windows.keys()) != set(refs):
        raise ValueError('windows must give exactly one table for every candidate')
    windowings = []
    for ref in refs:
        if ref.roll != roll:
            raise ValueError(f'candidate belongs to another roll: {ref!r}')
        try:
            raw = load_raw(source, ref)
        except LookupError as exc:
            raise ValueError(f'candidate revision does not exist: {ref!r}') from exc
        windowings.append(_normalize_window_table(windows[ref], raw))
    manifest = WindowedManifest(uuid4().hex, roll, slot_tuple, tuple(windowings))
    issuer.execute('BEGIN IMMEDIATE')
    with issuer:
        issuer.execute(
            'INSERT INTO manifests (manifest_id, payload) VALUES (?, ?)',
            (manifest.manifest_id, manifest.to_bytes()),
        )
    return manifest


def read_manifest(issuer: sqlite3.Connection, manifest_id: str):
    require_name(manifest_id)
    row = issuer.execute('SELECT payload FROM manifests WHERE manifest_id = ?', (manifest_id,)).fetchone()
    if row is None:
        raise LookupError(f'unknown manifest {manifest_id!r}')
    return manifest_from_bytes(row[0])


def read_windowed_manifest(issuer: sqlite3.Connection, manifest_id: str) -> WindowedManifest:
    manifest = read_manifest(issuer, manifest_id)
    if not isinstance(manifest, WindowedManifest):
        raise ValueError(f'manifest {manifest_id!r} is not a windowed freeze')
    return manifest
