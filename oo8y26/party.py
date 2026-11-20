"""一機五控的保守組隊規劃；已有跟隨狀態的角色不再發組隊要求。

目前只產生步驟，不自行送封包。每一步須收到新狀態或成功回應才能繼續。
實機一鍵組隊會對已入隊角色回錯，因此不使用 AT 批次重送。
"""
from dataclasses import dataclass, field, replace

from .session import ProtocolError


@dataclass(frozen=True)
class RoleState:
    slot: int
    status: int  # 0 離線、1 在線、2 跟隨；已由 GUI 與 L 回包對照。
    active: bool
    name: bytes = field(repr=False)
    level: int | None = 1
    rebirth: int | None = 0
    map_name: bytes = field(default=b"", repr=False)
    team: int = -1       # 所屬隊伍的隊長編號（L 回包每人第 12 欄；沒有隊伍是 -1）


@dataclass(frozen=True)
class PartyStep:
    slot: int
    action: str
    command: bytes


def parse_roles(message: bytes, *, active_slot: int | None = None) -> tuple[RoleState, ...]:
    """解析 func 2024 的 L|線路編號|每人13欄|，拒絕重複槽位與異常狀態。"""
    try:
        parts = message.split(b"|")
        if parts[0] != b"L" or parts[-1] != b"":
            raise ValueError
        # 第 1 段是目前連線所在的線路編號（一線 5、二線 6），不是筆數；
        # 2026-11-17 在二線實測收到 `L|6|` 加五筆資料。筆數改由長度推算。
        line_id = int(parts[1])
        count, remainder = divmod(len(parts) - 3, 13)
        if not 1 <= line_id <= 99 or remainder or not 1 <= count <= 5:
            raise ValueError
        roles = []
        for i in range(count):
            entry = parts[2 + i * 13:2 + (i + 1) * 13]
            slot, status, active = int(entry[0]), int(entry[5]), int(entry[6])
            if entry[1] in (b"", b"-1"):
                if status != -1 or active != -1 or int(entry[4]) != -1 or not 1 <= slot <= 5:
                    raise ValueError
                roles.append(RoleState(slot, 0, False, b"", None, None))
                continue
            if not 1 <= slot <= 5 or status not in (0, 1, 2, 3) or active not in (0, 1, 2):
                raise ValueError
            if not entry[1] or len(entry[1]) > 48:
                raise ValueError
            rebirth, level = int(entry[3]), int(entry[4])
            if not -1 <= rebirth <= 1000 or not (level == -1 or 1 <= level <= 1000):
                raise ValueError
            roles.append(RoleState(slot, status, slot == active_slot if active_slot is not None else active == 1, entry[1],
                                   None if level == -1 else level, None if rebirth == -1 else rebirth, entry[10],
                                   int(entry[11]) if entry[11].lstrip(b"-").isdigit() else -1))
        if len({role.slot for role in roles}) != count or sum(role.active for role in roles) != 1:
            raise ValueError
        return tuple(roles)
    except (ValueError, IndexError):
        raise ProtocolError("一機五控角色狀態異常，已停止") from None


def plan_party(roles: tuple[RoleState, ...], *, leader: int = 1,
               members: tuple[int, ...] = (2, 3, 4, 5)) -> tuple[PartyStep, ...]:
    """只規劃缺少的步驟；角色清單不完整或隊長不符時不猜測、不發指令。"""
    by_slot = {role.slot: role for role in roles}
    if len(by_slot) != len(roles):
        raise ProtocolError("角色狀態含重複槽位，已停止")
    if len(set(members)) != len(members) or leader in members:
        raise ProtocolError("隊伍設定含重複角色，已停止")
    if leader not in by_slot or not by_slot[leader].active or by_slot[leader].status != 1:
        raise ProtocolError("目前操作角色不是指定隊長，已停止")
    if any(slot not in by_slot for slot in members):
        raise ProtocolError("隊員狀態不完整，已停止")
    if any(not by_slot[slot].name for slot in (leader,) + members):
        raise ProtocolError("指定槽位尚未建立角色，已停止")
    steps = []
    for slot in members:
        role = by_slot[slot]
        if role.status == 2:
            continue
        # 實機 AL 登入後完整 L 清單會回狀態 3，仍需正常召喚、組隊。
        # 不把這個狀態誤判成必須先下線的獨立掛機。
        if role.status == 0:
            steps.append(PartyStep(slot, "登入", f"Q|{slot}".encode()))
        steps.append(PartyStep(slot, "召喚", f"Z|{slot}".encode()))
        steps.append(PartyStep(slot, "組隊", f"T|{slot}".encode()))
    return tuple(steps)


class RoleBook:
    """一機五控完整清單與單人狀態更新；不把文字提示當成成功確認。"""

    def __init__(self, controlled_slot=None):
        self.roles = {}
        self.revision = 0
        self.last_reply = None
        self.controlled_slot = controlled_slot
        self.pending = {}

    def apply(self, message: bytes):
        try:
            parts = message.rstrip(b"|").split(b"|")
            tag = parts[0]
            if tag == b"L":
                roles = parse_roles(message, active_slot=self.controlled_slot)
                self.roles = {role.slot: role for role in roles}
                self.pending.clear()  # 完整清單比先前收到的零碎推送新，不回套舊資料。
            elif tag in (b"I", b"Z", b"T", b"G", b"O", b"A"):
                slot = int(parts[1])
                counts = {b"I": (11,), b"Z": (3,), b"T": (3,), b"G": (2,), b"O": (2,), b"A": (7, 8)}
                if not 1 <= slot <= 5 or len(parts) not in counts[tag]:
                    raise ValueError
                if tag in (b"I", b"A") and int(parts[2]) not in (0, 1, 2, 3):
                    raise ValueError
                if tag == b"T" and (not parts[2].lstrip(b"-").isdigit() or int(parts[2]) < -1):
                    raise ValueError
                if slot not in self.roles:
                    self.pending[slot] = message
                    self.revision += 1
                    self.last_reply = (tag, slot)
                    return True
                role = self.roles[slot]
                if tag == b"I":
                    if len(parts) != 11:
                        raise ValueError
                    status, rebirth, level = map(int, parts[2:5])
                    if status not in (0, 1, 2, 3) or not 0 <= rebirth <= 1000 or not 1 <= level <= 1000:
                        raise ValueError
                    self.roles[slot] = replace(role, status=status, rebirth=rebirth, level=level, map_name=parts[5])
                elif tag == b"Z":
                    if len(parts) != 3:
                        raise ValueError
                    self.roles[slot] = replace(role, map_name=parts[2])
                elif tag == b"T":
                    if len(parts) != 3 or int(parts[2]) < -1:
                        raise ValueError
                    self.roles[slot] = replace(role, status=1 if int(parts[2]) == -1 else 2)
                elif tag in (b"G", b"O"):
                    if len(parts) != 2:
                        raise ValueError
                    self.roles[slot] = replace(role, status=3 if tag == b"G" else 0)
                else:
                    if len(parts) not in (7, 8):
                        raise ValueError
                    status = int(parts[2])
                    if status not in (0, 1, 2, 3):
                        raise ValueError
                    self.roles[slot] = replace(role, status=status)
            else:
                # 其他控制標籤尚不作為任何步驟的成功確認。
                return False
            self.revision += 1
            self.last_reply = (tag, None if tag == b"L" else int(parts[1]))
            return True
        except (ValueError, IndexError):
            raise ProtocolError("一機五控更新資料異常，已停止") from None

    def ready(self, leader=1, members=(2, 3, 4, 5)) -> bool:
        if leader not in self.roles or not self.roles[leader].active or self.roles[leader].status != 1:
            return False
        # 被換下隊長的那一隻在名冊上是「掛機」（狀態 3）但仍在隊裡：隊伍編號等於隊長就算（2026-11-19 切換角色後實測）。
        return all(slot in self.roles and (self.roles[slot].status == 2
                                          or (self.roles[slot].status == 3 and self.roles[slot].team == leader))
                   for slot in members)

    def minimum_level(self, slots=(1, 2, 3, 4, 5)) -> int:
        if any(slot not in self.roles or self.roles[slot].level is None for slot in slots):
            raise ProtocolError("隊伍等級資料不完整，已停止")
        return min(self.roles[slot].level for slot in slots)


class PartyProgress:
    """每個步驟只允許取出一次；沒有成功確認就禁止繼續，也不重送。"""

    def __init__(self, steps: tuple[PartyStep, ...]):
        self.steps = steps
        self.index = 0
        self.pending = False
        self.stopped = False

    def next_step(self) -> PartyStep | None:
        if self.stopped or self.pending:
            self.stopped = True
            raise ProtocolError("組隊已停止或尚未收到成功確認，禁止續送")
        if self.index >= len(self.steps):
            return None
        self.pending = True
        return self.steps[self.index]

    def confirm(self, *, success: bool):
        if self.stopped or not self.pending:
            self.stopped = True
            raise ProtocolError("沒有可確認的組隊步驟，已停止")
        if not success:
            self.stopped = True
            raise ProtocolError("組隊步驟失敗，已停止；不重試")
        self.pending = False
        self.index += 1
