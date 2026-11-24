import sqlite3
from dataclasses import dataclass

from receipt.integrity import VerifiedPiece, verify_piece
from source.locations import is_positive_id


@dataclass(frozen=True, slots=True)
class AcceptedPiece:
    accepted_id: int
    verified: VerifiedPiece


def open_inventory(path) -> sqlite3.Connection:
    store = sqlite3.connect(path, isolation_level=None)
    try:
        store.execute('''CREATE TABLE IF NOT EXISTS accepted_pieces (
            accepted_id INTEGER PRIMARY KEY,
            piece_id TEXT NOT NULL UNIQUE,
            payload BLOB NOT NULL
        )''')
    except BaseException:
        store.close()
        raise
    return store


def _from_row(row: tuple) -> AcceptedPiece:
    accepted_id, piece_id, payload = row
    verified = verify_piece(payload)
    if verified.piece_id != piece_id:
        raise ValueError('stored piece identity mismatch')
    return AcceptedPiece(accepted_id, verified)


def find_by_piece_id(store: sqlite3.Connection, piece_id: str) -> AcceptedPiece | None:
    row = store.execute(
        'SELECT accepted_id, piece_id, payload FROM accepted_pieces WHERE piece_id = ?', (piece_id,)
    ).fetchone()
    return None if row is None else _from_row(row)


def insert_accepted(store: sqlite3.Connection, piece: VerifiedPiece) -> AcceptedPiece:
    if not store.in_transaction:
        raise RuntimeError('insert_accepted requires an inventory transaction')
    cursor = store.execute(
        'INSERT INTO accepted_pieces (piece_id, payload) VALUES (?, ?)', (piece.piece_id, piece.payload)
    )
    return AcceptedPiece(cursor.lastrowid, piece)


def read_accepted(store: sqlite3.Connection, accepted_id: int) -> AcceptedPiece:
    if not is_positive_id(accepted_id):
        raise LookupError(f'invalid receipt number {accepted_id!r}')
    row = store.execute(
        'SELECT accepted_id, piece_id, payload FROM accepted_pieces WHERE accepted_id = ?', (accepted_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f'unknown receipt number {accepted_id!r}')
    return _from_row(row)
