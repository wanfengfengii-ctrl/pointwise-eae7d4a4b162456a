import sqlite3
from dataclasses import replace

from issue.eligibility import require_issuable_revision
from issue.manifests import open_manifest_store, read_manifest, read_windowed_manifest
from issue.segment_evidence import build_segment_evidence
from issue.wire import PieceEnvelope, PieceIdentity, encode_piece, piece_digest
from issue.wire_v2 import (
    KIND_BOUNDARY,
    KIND_WINDOW,
    V2BoundaryBody,
    V2Envelope,
    V2Identity,
    V2WindowBody,
    build_checkpoints,
    encode_v2,
    v2_digest,
)
from source.revisions import read_revision


def open_issued_store(path) -> sqlite3.Connection:
    store = open_manifest_store(path)
    try:
        store.execute('''CREATE TABLE IF NOT EXISTS issued_pieces (
            piece_id TEXT PRIMARY KEY NOT NULL,
            manifest_id TEXT NOT NULL REFERENCES manifests (manifest_id),
            segment TEXT NOT NULL,
            revision INTEGER NOT NULL,
            payload BLOB NOT NULL,
            UNIQUE (manifest_id, segment, revision)
        )''')
        store.execute('''CREATE TABLE IF NOT EXISTS issued_windowed_pieces (
            piece_id TEXT PRIMARY KEY NOT NULL,
            manifest_id TEXT NOT NULL REFERENCES manifests (manifest_id),
            segment TEXT NOT NULL,
            revision INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('boundary', 'window')),
            window_name TEXT NOT NULL DEFAULT '',
            payload BLOB NOT NULL,
            UNIQUE (manifest_id, segment, revision, kind, window_name)
        )''')
    except BaseException:
        store.close()
        raise
    return store


def issue(
    source: sqlite3.Connection,
    issuer: sqlite3.Connection,
    manifest_id: str,
    segment: str,
    revision: int,
) -> bytes:
    manifest = read_manifest(issuer, manifest_id)
    ref = require_issuable_revision(manifest, segment, revision)
    identity = PieceIdentity(manifest.manifest_id, ref)
    key = (manifest_id, ref.segment, ref.revision)
    issuer.execute('BEGIN IMMEDIATE')
    with issuer:
        existing = issuer.execute(
            '''SELECT payload FROM issued_pieces
               WHERE manifest_id = ? AND segment = ? AND revision = ?''', key
        ).fetchone()
        if existing is not None:
            return existing[0]
        evidence = build_segment_evidence(read_revision(source, ref).raw)
        payload = encode_piece(PieceEnvelope(identity, evidence, piece_digest(identity, evidence)))
        issuer.execute(
            '''INSERT INTO issued_pieces (manifest_id, segment, revision, piece_id, payload)
               VALUES (?, ?, ?, ?, ?)''', (*key, identity.piece_id, payload)
        )
    return payload


def _build_boundary_payload(source, manifest, ref) -> tuple[str, bytes]:
    raw = read_revision(source, ref).raw
    binding = manifest.windowing_for(ref)
    if binding is None:
        raise ValueError(f'no frozen window table for {ref!r}')
    coords = tuple((item.name, item.start, item.end) for item in binding.windows)
    body = V2BoundaryBody(
        raw.body_start, raw.body_end, coords, build_checkpoints(raw, coords)
    )
    identity = V2Identity(manifest.manifest_id, ref, None)
    envelope = V2Envelope(KIND_BOUNDARY, identity.piece_id, identity, body, '')
    envelope = replace(envelope, digest=v2_digest(envelope))
    return identity.piece_id, encode_v2(envelope)


def _build_window_payload(source, manifest, ref, window_name: str) -> tuple[str, bytes]:
    binding = manifest.windowing_for(ref)
    if binding is None:
        raise ValueError(f'no frozen window table for {ref!r}')
    coord = binding.window(window_name)
    if coord is None:
        raise ValueError(f'window {window_name!r} is not frozen for {ref!r}')
    raw = read_revision(source, ref).raw
    data = raw.raw_bytes[coord.start:coord.end]
    body = V2WindowBody(coord.name, coord.start, coord.end, data)
    identity = V2Identity(manifest.manifest_id, ref, coord.name)
    envelope = V2Envelope(KIND_WINDOW, identity.piece_id, identity, body, '')
    envelope = replace(envelope, digest=v2_digest(envelope))
    return identity.piece_id, encode_v2(envelope)


def issue_windowed(
    source: sqlite3.Connection,
    issuer: sqlite3.Connection,
    manifest_id: str,
    segment: str,
    revision: int,
    *,
    window: str | None = None,
) -> bytes:
    manifest = read_windowed_manifest(issuer, manifest_id)
    ref = require_issuable_revision(manifest, segment, revision)
    if window is None:
        kind = 'boundary'
        window_name = ''
    else:
        from source.locations import require_name

        require_name(window)
        kind = 'window'
        window_name = window
    key = (manifest_id, ref.segment, ref.revision, kind, window_name)
    issuer.execute('BEGIN IMMEDIATE')
    with issuer:
        existing = issuer.execute(
            '''SELECT payload FROM issued_windowed_pieces
               WHERE manifest_id = ? AND segment = ? AND revision = ?
                 AND kind = ? AND window_name = ?''',
            key,
        ).fetchone()
        if existing is not None:
            return existing[0]
        if window is None:
            piece_id, payload = _build_boundary_payload(source, manifest, ref)
        else:
            piece_id, payload = _build_window_payload(source, manifest, ref, window)
        issuer.execute(
            '''INSERT INTO issued_windowed_pieces
                   (piece_id, manifest_id, segment, revision, kind, window_name, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (piece_id, manifest_id, ref.segment, ref.revision, kind, window_name, payload),
        )
    return payload


def reissue(issuer: sqlite3.Connection, piece_id: str) -> bytes:
    row = issuer.execute('SELECT payload FROM issued_pieces WHERE piece_id = ?', (piece_id,)).fetchone()
    if row is not None:
        return row[0]
    row = issuer.execute(
        'SELECT payload FROM issued_windowed_pieces WHERE piece_id = ?', (piece_id,)
    ).fetchone()
    if row is not None:
        return row[0]
    raise LookupError(f'unknown issued piece {piece_id!r}')
