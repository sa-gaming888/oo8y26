"""經校驗遊戲事件的狀態投影：地圖、座標、生命與隊伍等級。"""
import time
from dataclasses import dataclass, field

from .party import RoleBook
from .session import ProtocolError

_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def decode_mask(value: bytes) -> int:
    """人物與寵物狀態使用小寫優先、最高位在前的 base62 位元遮罩。"""
    if not value or len(value) > 12:
        raise ValueError("狀態遮罩長度錯誤")
    result = 0
    for char in value.decode("ascii"):
        result = result * 62 + _BASE62.index(char)
    if result >= 1 << 64:
        raise ValueError("狀態遮罩超過 64 位元")
    return result


BOUND_MARK = "绑定物品".encode("gbk")


@dataclass
class PetState:
    index: int
    name: bytes = field(repr=False)
    graphic: int
    hp: int
    max_hp: int
    level: int
    work_state: int | None = None


@dataclass
class WorldState:
    map_id: int | None = None
    width: int = 0
    height: int = 0
    x: int | None = None
    y: int | None = None
    hp: int | None = None
    max_hp: int | None = None
    mp: int | None = None
    max_mp: int | None = None
    actor_id: int | None = None
    client_flags: int | None = None
    level: int | None = None
    mount_pet_index: int | None = None
    battle_pet_index: int | None = None
    pets: dict[int, PetState] = field(default_factory=dict, repr=False)
    server_time: int | None = None       # 登入時伺服器給的 Unix 秒
    server_time_at: float = 0.0          # 收到當下的本機時間
    pet_skills: dict = field(default_factory=dict, repr=False)   # {寵物欄: [各技能欄的名稱，空欄是空字串]}
    companions: dict[int, "WorldState"] = field(default_factory=dict, repr=False)
    roles: RoleBook = field(default_factory=RoleBook, repr=False)
    tasks: tuple | None = field(default=None, repr=False)   # S809 原文（完成位元, 步驟進度），由 oo8y26.quest 解析
    name: bytes | None = field(default=None, repr=False)    # 自己的角色名（P1 第 29 欄），用來在 S41 裡認出自己
    bound: set = field(default_factory=set, repr=False)     # 綁定道具所在的背包格（第 9 欄是「绑定物品」），不能存共享倉庫
    items: dict | None = field(default=None, repr=False)    # 背包 {格: (名稱, 堆疊數)}；0～8 是裝備格，9 起是背包
    raw: dict = field(default_factory=dict, repr=False)     # 最近一次的完整狀態原文（切好的欄位）：P1、K0～K4、I、A；面板查詢用

    def apply(self, event):
        if event.func == 46:
            self._items(event.values[0])
            self._status(event.values[0])
        elif event.func == 22:
            self._item(event.values[0])
        elif event.func == 41:
            self._seen(event.values[0])
        elif event.func == 809:
            self.tasks = tuple(event.values[:2])
        elif event.func == 262:
            # 開商城視窗或換分頁時送的貨幣：第 0 個是石幣、第 2 個是聲望（2026-11-20 對照畫面）；其餘還沒對出來。
            self.raw["貨幣"] = tuple(event.values)
        elif event.func == 49:
            flags = event.values[0]
            if not 0 <= flags <= 0x7fffffff:
                raise ProtocolError("角色旗標數值異常，已停止")
            self.client_flags = flags
        elif event.func == 2024:
            self.roles.apply(event.values[0])
        elif event.func == 2026:
            owner, message = event.values
            if owner == 0 and message.startswith(b"E|"):
                return  # 已由事件解碼器先檢查人工驗證及操作失敗。
            actor = self.actor(owner)
            actor._status(message)
            if actor.level is not None and owner in self.roles.roles:
                from dataclasses import replace
                self.roles.roles[owner] = replace(self.roles.roles[owner], level=actor.level)
        elif event.func in (55, 2015):
            if event.func == 55:
                actor, (index, success) = self, event.values
            else:
                owner, index, success = event.values
                actor = self.actor(owner)
            if success != 1 or not -1 <= index <= 4:
                raise ProtocolError("戰寵設定未成功或索引異常，已停止")
            actor.battle_pet_index = index
        elif event.func == 2018:
            owner, index, state = event.values
            actor = self.actor(owner)
            if not 0 <= index <= 4 or state not in (0, 1):
                raise ProtocolError("寵物待機狀態異常，已停止")
            if index in actor.pets:
                actor.pets[index].work_state = state

    def _seen(self, message: bytes):
        """S41 周圍角色清單：走路時伺服器不另外回推座標，但每走一段會送一筆自己的資料（第 3、4 欄是座標）。"""
        if self.name is None or self.width <= 0:
            return
        for entry in message.split(b","):
            fields = entry.split(b"|")
            if len(fields) > 9 and fields[1] == b"1" and fields[9] == self.name and fields[3].isdigit() and fields[4].isdigit():
                x, y = int(fields[3]), int(fields[4])
                if 0 <= x < self.width and 0 <= y < self.height:
                    self.x, self.y = x, y

    def _items(self, message: bytes):
        """狀態訊息 `I`：54 格 × 23 欄（第 0 欄名稱、第 10 欄堆疊數）。格式不符就不更新。"""
        if not message.startswith(b"I"):
            return
        parts = message[1:].split(b"|")
        if len(parts) != 54 * 23:
            return
        try:
            self.items = {index // 23: (parts[index].decode("gbk"), int(parts[index + 10] or 0))
                          for index in range(0, len(parts), 23) if parts[index]}
            self.raw["I"] = parts
            self.bound = {index // 23 for index in range(0, len(parts), 23)
                          if parts[index] and parts[index + 9] == BOUND_MARK}
        except (UnicodeError, ValueError):
            self.items = None

    def _item(self, message: bytes):
        """S22 單格道具更新：`格|名稱|…`（格後面同樣 23 欄）；名稱為空表示該格清空。"""
        parts = message.split(b"|")
        # 清空某一格時伺服器少送一欄（實測 `23|` 後面 22 個分隔符號），兩種長度都要認。
        if self.items is None or len(parts) not in (23, 24) or not parts[0].isdigit():
            return
        try:
            slot = int(parts[0])
            whole = self.raw.get("I")
            if whole is not None and 0 <= slot < 54:
                fresh = parts[1:24] + [b""] * (23 - len(parts[1:24]))
                whole[slot * 23:slot * 23 + 23] = fresh if parts[1] else [b""] * 23
            self.bound.discard(slot)
            if parts[1]:
                self.items[slot] = (parts[1].decode("gbk"), int(parts[11] or 0))
                if parts[10] == BOUND_MARK:
                    self.bound.add(slot)
            else:
                self.items.pop(slot, None)
        except (UnicodeError, ValueError):
            self.items = None

    def has_item(self, name: str) -> bool:
        if self.items is None:
            raise ProtocolError("沒有收到背包資料，已停止")
        return any(item == name for item, _ in self.items.values())

    def sa_hour(self, now=None):
        """遊戲內的時刻（0～1023）。原版石器的算法：一天 5400 秒、分成 1024 個時刻，起算點 912766409；
        300～700 是夜晚、700～930 早上、930～200 中午、200～300 傍晚。還不知道伺服器時間就回 None。
        本遊戲是否完全照原版還沒有用畫面對照過（2026-11-19）。"""
        if self.server_time is None:
            return None
        seconds = self.server_time + int((now if now is not None else time.time()) - self.server_time_at) - 912766409
        return seconds % 5400 * 1024 // 5400

    def night(self, now=None):
        hour = self.sa_hour(now)
        return None if hour is None else 300 < hour <= 700

    def has_pet(self, name: str, level: int = 0) -> bool:
        return any(pet.name.decode("gbk", "replace") == name and pet.level >= level for pet in self.pets.values())

    def free_bag_slots(self, *, first=9, last=23) -> int:
        if self.items is None:
            raise ProtocolError("沒有收到背包資料，已停止")
        return sum(1 for slot in range(first, last + 1) if slot not in self.items)

    def actor(self, slot):
        if not 1 <= slot <= 5:
            raise ProtocolError("子號狀態超出 1 至 5，已停止")
        if slot == self.roles.controlled_slot:
            return self
        return self.companions.setdefault(slot, WorldState())

    def _validate_health(self):
        if self.hp is not None and self.max_hp is not None:
            if not 0 <= self.hp <= self.max_hp <= 10000000 or self.max_hp <= 0:
                raise ValueError
        if self.mp is not None and self.max_mp is not None:
            if not 0 <= self.mp <= self.max_mp <= 10000000:
                raise ValueError

    def _status(self, message: bytes):
        parts = message.split(b"|")
        try:
            if parts[0].startswith(b"C") and parts[0][1:].isdigit():
                if len(parts) != 5:
                    raise ValueError
                floor = int(parts[0][1:])
                width, height, x, y = map(int, parts[1:])
                if not (0 < floor < 1000000 and 0 < width <= 8192 and 0 < height <= 8192
                        and 0 <= x < width and 0 <= y < height):
                    raise ValueError
                self.map_id, self.width, self.height, self.x, self.y = floor, width, height, x, y
            elif parts[0] == b"P1":
                if len(parts) != 56:
                    raise ValueError
                hp, max_hp, mp, max_mp = map(int, parts[1:5])
                if not (0 <= hp <= max_hp <= 10000000 and max_hp > 0 and 0 <= mp <= max_mp <= 10000000):
                    raise ValueError
                self.hp, self.max_hp, self.mp, self.max_mp = hp, max_hp, mp, max_mp
                self.level = int(parts[11])
                self.exp_fields = (parts[9], parts[10])      # 推測是目前經驗、升級所需經驗（練功時拿來看有沒有在漲）
                self.name = parts[29] or None
                self.mount_pet_index = int(parts[25])
                if not 1 <= self.level <= 1000 or not -1 <= self.mount_pet_index <= 4:
                    raise ValueError
                self.raw["P1"] = parts
            elif parts[0].startswith(b"P"):
                mask = decode_mask(parts[0][1:])
                bits = [bit for bit in range(64) if mask & (1 << bit)]
                if mask & 1 or not bits or parts[-1] != b"" or len(parts) != len(bits) + 2:
                    raise ValueError
                updates = dict(zip(bits, parts[1:-1]))
                whole = self.raw.get("P1")
                if whole is not None:
                    for bit, value in updates.items():
                        if bit < len(whole):
                            whole[bit] = value
                if 9 in updates or 10 in updates:
                    old = getattr(self, "exp_fields", (b"", b""))
                    self.exp_fields = (updates.get(9, old[0]), updates.get(10, old[1]))
                for bit, attribute in ((1, "hp"), (2, "max_hp"), (3, "mp"), (4, "max_mp"),
                                       (11, "level"), (27, "mount_pet_index")):
                    if bit in updates:
                        value = int(updates[bit])
                        if attribute == "level" and not 1 <= value <= 1000:
                            raise ValueError
                        if attribute == "mount_pet_index" and not -1 <= value <= 4:
                            raise ValueError
                        setattr(self, attribute, value)
                self._validate_health()
            elif parts[0] == b"A" and len(parts) == 8:
                self.raw["A"] = parts
            elif parts[0].startswith(b"D") and parts[0][1:].isdigit() and len(parts) == 2:
                # `D角色編號|伺服器時間`（Unix 秒）：遊戲內的日夜由它算，見 sa_hour()。
                self.server_time, self.server_time_at = int(parts[1]), time.time()
            elif parts[0].startswith(b"W") and parts[0][1:].isdigit():
                # 寵物技能欄：每欄 6 個欄位（編號、?、?、名稱、說明、?），空欄全空白。
                index = int(parts[0][1:])
                if not 0 <= index <= 4:
                    raise ValueError
                self.pet_skills[index] = [parts[4 + slot * 6].decode("gbk", "replace")
                                          for slot in range((len(parts) - 1) // 6)]
            elif parts[0].startswith(b"K") and parts[0][1:].isdigit():
                index = int(parts[0][1:])
                if not 0 <= index <= 4:
                    raise ValueError
                mask = decode_mask(parts[1])
                if mask == 0:
                    if parts != [parts[0], b"0", b""]:
                        raise ValueError
                    self.pets.pop(index, None)
                    self.raw.pop(f"K{index}", None)
                elif mask == 1:
                    if len(parts) not in (55, 58):      # 剛抓到的寵物少三欄（成長率還沒算出來；2026-11-19 加克拉實測）
                        raise ValueError
                    graphic, hp, max_hp, level = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[9])
                    if 0 < max_hp < hp <= max_hp + max_hp // 4 + 10:
                        hp = max_hp      # 剛從個人倉庫領出的寵物，血量會比滿血多幾點（2026-11-19 金布伊 51/49）
                    if not (0 <= hp <= max_hp <= 10000000 and max_hp > 0 and 1 <= level <= 1000 and parts[22]):
                        raise ValueError
                    previous = self.pets.get(index)
                    self.pets[index] = PetState(index, parts[22], graphic, hp, max_hp, level,
                                                previous.work_state if previous else None)
                    self.raw[f"K{index}"] = parts
                else:
                    bits = [bit for bit in range(64) if mask & (1 << bit)]
                    expected = len(bits) + 3
                    # 後面有時多帶 4 個「0,0」欄位（裝備格）：先前只在位元 16 看過，2026-11-19 吃補血道具後的
                    # `K2|148|989|100|0,0|0,0|0,0|0,0|`（位元 2、12）也有，所以不限位元。
                    valid_count = len(parts) in (expected, expected + 4)
                    if mask & 1 or parts[-1] != b"" or not valid_count:
                        raise ValueError
                    updates = dict(zip(bits, parts[2:-1]))
                    if index not in self.pets:
                        return        # 剛抓到的寵物會先收到部分更新、之後才是完整資料（2026-11-19 抓到加格時實測）
                    pet = self.pets[index]
                    whole = self.raw.get(f"K{index}")
                    if whole is not None:
                        for bit, value in updates.items():      # 位元 k 對應完整訊息的第 k＋1 段
                            if bit + 1 < len(whole):
                                whole[bit + 1] = value
                    if 2 in updates:
                        pet.hp = int(updates[2])
                    if 3 in updates:
                        pet.max_hp = int(updates[3])
                    if not 0 <= pet.hp <= pet.max_hp or pet.max_hp <= 0:
                        raise ValueError
        except (ValueError, IndexError, UnicodeError):
            head = message[:80].decode("gbk", "replace")
            raise ProtocolError(f"角色地圖、座標或生命數值異常（{head}），已停止") from None
