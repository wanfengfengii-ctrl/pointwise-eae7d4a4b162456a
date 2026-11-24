import sqlite3

from receipt.deduplication import DeliveryDisposition, classify_delivery
from receipt.integrity import verify_piece
from receipt.inventory import find_by_piece_id, insert_accepted


def receive(inventory: sqlite3.Connection, payload: bytes) -> int:
    verified = verify_piece(payload)
    inventory.execute('BEGIN IMMEDIATE')
    with inventory:
        existing = find_by_piece_id(inventory, verified.piece_id)
        disposition = classify_delivery(verified, existing)
        if disposition is DeliveryDisposition.COLLISION:
            raise ValueError('different fixed content for an existing piece number')
        if disposition is DeliveryDisposition.FIRST:
            accepted = insert_accepted(inventory, verified)
        else:
            accepted = existing
    return accepted.accepted_id
