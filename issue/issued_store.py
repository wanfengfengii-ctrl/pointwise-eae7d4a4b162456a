import sqlite3

from issue.eligibility import require_issuable_revision
from issue.manifests import open_manifest_store, read_manifest
from issue.segment_evidence import build_segment_evidence
from issue.wire import PieceEnvelope, PieceIdentity, encode_piece, piece_digest
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


def reissue(issuer: sqlite3.Connection, piece_id: str) -> bytes:
    row = issuer.execute('SELECT payload FROM issued_pieces WHERE piece_id = ?', (piece_id,)).fetchone()
    if row is None:
        raise LookupError(f'unknown issued piece {piece_id!r}')
    return row[0]
