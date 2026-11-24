from dataclasses import replace

from assembly.close import close
from issue.wire import decode_piece, encode_piece, piece_digest
from receipt.intake import receive
from receipt.integrity import verify_piece
from receipt.inventory import open_inventory, read_accepted
from tests.support import HandoffCase


def change_reading(payload, *, refresh_digest):
    envelope = decode_piece(payload)
    ordinary = envelope.evidence.ordinary
    escaped = envelope.evidence.escaped
    changed = replace(
        envelope.evidence,
        ordinary=replace(ordinary, events=(replace(ordinary.events[0], value=9), *ordinary.events[1:])),
        escaped=replace(escaped, events=(replace(escaped.events[0], value=9), *escaped.events[1:])),
    )
    digest = piece_digest(envelope.identity, changed) if refresh_digest else envelope.digest
    return encode_piece(replace(envelope, evidence=changed, digest=digest))


class ReceiptTests(HandoffCase):
    def test_out_of_order_and_duplicate_delivery_across_reopen(self):
        manifest, (left, right), (left_ref, right_ref) = self.issue_recording(
            'P', (('L', b'\x07'), ('R', b'\x0b\n'))
        )
        self.disconnect_source()
        right_id = receive(self.inventory, right)
        self.inventory.close()
        self.inventory = self.open_db(open_inventory, 'accepted.db')
        left_id = receive(self.inventory, left)
        self.assertEqual(receive(self.inventory, right), right_id)
        self.assertEqual(self.accepted_count(), 2)
        output, first = close(self.inventory, self.reports, manifest, [right_id, left_id])
        self.assertEqual(first.accepted_ids, (left_id, right_id))
        self.assertEqual(tuple(item.revision for item in output.selections), (left_ref, right_ref))
        self.assertEqual(tuple(item.values for item in output.records), ((7, 11),))
        self.assertEqual(output.records[0].total, 18)

    def test_digest_mismatch_before_first_receipt(self):
        manifest, (left, right), _ = self.issue_recording('P', (('L', b'\x07'), ('R', b'\x0b\n')))
        self.disconnect_source()
        right_id = receive(self.inventory, right)
        changed = change_reading(left, refresh_digest=False)
        self.assertEqual(decode_piece(changed).identity, decode_piece(left).identity)
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            receive(self.inventory, changed)
        self.assertEqual(self.accepted_count(), 1)
        self.assertEqual(read_accepted(self.inventory, right_id).verified.payload, right)
        left_id = receive(self.inventory, left)
        result, _ = close(self.inventory, self.reports, manifest, [left_id, right_id])
        self.assertEqual(result.records[0].values, (7, 11))

    def test_same_id_collision_with_valid_digest(self):
        manifest, (payload,), _ = self.issue_recording('P', (('L', b'\x07\n'),))
        self.disconnect_source()
        original_id = receive(self.inventory, payload)
        self.assertEqual(receive(self.inventory, payload), original_id)
        changed = change_reading(payload, refresh_digest=True)
        self.assertNotEqual(changed, payload)
        self.assertEqual(verify_piece(changed).piece_id, verify_piece(payload).piece_id)
        with self.assertRaisesRegex(ValueError, 'different fixed content'):
            receive(self.inventory, changed)
        self.assertEqual(self.accepted_count(), 1)
        self.assertEqual(read_accepted(self.inventory, original_id).verified.payload, payload)
        result, _ = close(self.inventory, self.reports, manifest, [original_id])
        self.assertEqual((result.records[0].values, result.records[0].total), ((7,), 7))

    def test_bad_outer_envelope_does_not_create_a_receipt(self):
        _, (payload,), _ = self.issue_recording('P', (('S', b'\n'),))
        for bad in (b'[]', b'\xff', payload[:-1], payload + b' '):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                receive(self.inventory, bad)
        self.assertEqual(self.accepted_count(), 0)
