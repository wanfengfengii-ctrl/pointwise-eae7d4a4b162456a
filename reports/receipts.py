from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FirstReceipt:
    receipt_number: str
    report_id: int
    manifest_id: str
    accepted_ids: tuple[int, ...]


def make_first_receipt(report_id: int, manifest_id: str, accepted_ids: tuple[int, ...]) -> FirstReceipt:
    return FirstReceipt(f'F-{report_id}', report_id, manifest_id, accepted_ids)
