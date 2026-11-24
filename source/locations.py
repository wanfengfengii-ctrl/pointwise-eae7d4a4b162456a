from dataclasses import dataclass


MAX_SQLITE_ID = (1 << 63) - 1


def require_name(value: str) -> None:
    if type(value) is not str or not value or value != value.strip() or '\x00' in value:
        raise ValueError('expected a nonempty name without padding or NUL')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('name is not valid UTF-8 text') from exc


def is_positive_id(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_SQLITE_ID


@dataclass(frozen=True, slots=True)
class RevisionRef:
    roll: str
    segment: str
    revision: int

    def __post_init__(self) -> None:
        require_name(self.roll)
        require_name(self.segment)
        if not is_positive_id(self.revision):
            raise ValueError('revision must be a positive integer')

    def to_list(self) -> list:
        return [self.roll, self.segment, self.revision]

    @classmethod
    def from_list(cls, data: object) -> 'RevisionRef':
        if type(data) is not list or len(data) != 3:
            raise ValueError('invalid revision reference')
        return cls(*data)


@dataclass(frozen=True, slots=True)
class ByteLocation:
    revision: RevisionRef
    offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.revision, RevisionRef) or type(self.offset) is not int or self.offset < 0:
            raise ValueError('invalid original byte location')

    def to_list(self) -> list:
        return [self.revision.to_list(), self.offset]

    @classmethod
    def from_list(cls, data: object) -> 'ByteLocation':
        if type(data) is not list or len(data) != 2:
            raise ValueError('invalid original byte location')
        return cls(RevisionRef.from_list(data[0]), data[1])


def locate_byte(ref: RevisionRef, body_offset: int, body_start: int, body_length: int) -> ByteLocation:
    if type(body_offset) is not int or not 0 <= body_offset < body_length:
        raise ValueError('byte is outside the recording body')
    return ByteLocation(ref, body_start + body_offset)
