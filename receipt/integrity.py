from dataclasses import dataclass
from hmac import compare_digest

from issue.wire import PieceEnvelope, decode_piece, piece_digest
from issue.wire_v2 import V2Envelope, decode_v2, is_v2_payload, v2_digest


@dataclass(frozen=True, slots=True)
class VerifiedPiece:
    payload: bytes
    envelope: PieceEnvelope | V2Envelope

    @property
    def piece_id(self) -> str:
        return self.envelope.identity.piece_id


def verify_piece(payload: bytes) -> VerifiedPiece:
    if is_v2_payload(payload):
        envelope = decode_v2(payload)
        expected = v2_digest(envelope)
        if not compare_digest(envelope.digest, expected):
            raise ValueError('piece digest mismatch')
        return VerifiedPiece(payload, envelope)
    envelope = decode_piece(payload)
    expected = piece_digest(envelope.identity, envelope.evidence)
    if not compare_digest(envelope.digest, expected):
        raise ValueError('piece digest mismatch')
    return VerifiedPiece(payload, envelope)
