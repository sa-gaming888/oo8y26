"""解析貼心傳送清單；目的地代號只採用本次伺服器回覆。"""
from dataclasses import dataclass

from .session import ProtocolError


@dataclass(frozen=True)
class Destination:
    name: str
    code: int
    requirement: int
    rebirth_requirement: int = -1


def parse_destinations(message: bytes, category: int):
    try:
        parts = message.decode("gbk").split("|")
        if len(parts) < 3 or parts[:2] != ["I", str(category)]:
            raise ValueError
        count = int(parts[2])
        if not 0 <= count <= 100 or len(parts) != 3 + count * 4:
            raise ValueError
        entries = []
        for start in range(3, len(parts), 4):
            name, code, rebirth, requirement = parts[start:start + 4]
            code, requirement, rebirth = int(code), int(requirement), int(rebirth)
            if not name.strip() or not 1 <= code <= 1000000 or not -1 <= rebirth <= 100 or not -1 <= requirement <= 1000:
                raise ValueError
            entries.append(Destination(name.strip(), code, requirement, rebirth))
        if len({entry.code for entry in entries}) != count:
            raise ValueError
        return tuple(entries)
    except (ValueError, UnicodeError):
        raise ProtocolError("貼心傳送清單格式不符，已停止") from None
