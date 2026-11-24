from enum import Enum, auto

from receipt.integrity import VerifiedPiece
from receipt.inventory import AcceptedPiece


class DeliveryDisposition(Enum):
    FIRST = auto()
    REPEAT = auto()
    COLLISION = auto()


def classify_delivery(piece: VerifiedPiece, existing: AcceptedPiece | None) -> DeliveryDisposition:
    if existing is None:
        return DeliveryDisposition.FIRST
    if piece.payload == existing.verified.payload:
        return DeliveryDisposition.REPEAT
    return DeliveryDisposition.COLLISION
