from dataclasses import dataclass
from hmac import compare_digest

from issue.wire import PieceEnvelope, decode_piece, piece_digest


@dataclass(frozen=True, slots=True)
class VerifiedPiece:
    payload: bytes
    envelope: PieceEnvelope

    @property
    def piece_id(self) -> str:
        return self.envelope.identity.piece_id


def verify_piece(payload: bytes) -> VerifiedPiece:
    envelope = decode_piece(payload)
    expected = piece_digest(envelope.identity, envelope.evidence)
    if not compare_digest(envelope.digest, expected):
        raise ValueError('piece digest mismatch')
    return VerifiedPiece(payload, envelope)
