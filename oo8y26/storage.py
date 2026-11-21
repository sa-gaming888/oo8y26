"""共享寵物倉庫：把用不到的寵物存進去、之後再領出來（同帳號的角色共用，容量由伺服器回報）。

2026-11-18 對照客戶端 `lua/win/1124.xy` 與擷取，並線上實測：
- 開啟：C202(36) → S66 (1124, 0, 0, objindex, "L|已存數|容量|名稱|等級|血|地|水|火|風|倉庫編號…", "公共宠物仓库", …)
- 存入：C67 (x, y, 0, objindex, 0, "I|寵物欄索引")；伺服器回 S808 的 "Z|名稱|…|倉庫編號" 並清掉該寵物欄
- 領出：C67 (…, "B|倉庫編號")；伺服器回 S808 的 "D|倉庫編號" 並推送新的寵物欄資料
- 關閉：C67 (…, "C|")
任何一步沒等到伺服器的確認就停，不重試。騎寵與戰寵不允許存入。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .session import ProtocolError, Session

WINDOW = 1124


@dataclass(frozen=True)
class StoredPet:
    name: str
    level: int
    key: str            # 倉庫編號（領出時要用）


def parse_listing(message: bytes):
    """解析 `L|已存數|容量|…每隻 8 欄`，回傳 (容量, [StoredPet…])。"""
    try:
        parts = message.decode("gbk").split("|")
        if parts[0] != "L":
            raise ValueError
        count, capacity = int(parts[1]), int(parts[2])
        if not 0 <= count <= capacity <= 1000 or len(parts) < 3 + count * 8:
            raise ValueError
        pets = [StoredPet(parts[3 + i * 8], int(parts[4 + i * 8]), parts[10 + i * 8]) for i in range(count)]
        if any(not pet.name or not pet.key for pet in pets):
            raise ValueError
        return capacity, pets
    except (ValueError, IndexError, UnicodeError):
        raise ProtocolError("寵物倉庫清單格式不符，已停止") from None


class PetStorage:
    def __init__(self, session: Session):
        self.session, self.objindex, self.capacity, self.pets = session, None, 0, []

    def _send(self, func, values):
        try:
            self.session._send_command(func, values, time.monotonic() + self.session.timeout)
        except BaseException:
            self.session.close()
            raise

    def _wait(self, done, seconds=6.0, watch=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not done():
            event = self.session.poll_event(0.3)
            if event is not None and watch is not None:
                watch(event)
        return done()

    def open(self):
        session = self.session
        if session.battle.active:
            raise ProtocolError("戰鬥中不可開寵物倉庫，已停止")
        session.sync()
        found = {}

        def watch(event):
            if event.func == 66 and event.values[0] == WINDOW:
                found["window"] = event

        self._send(202, (36,))
        if not self._wait(lambda: "window" in found, watch=watch):
            # 實測：上一次開了倉庫沒送關閉（C|）就做別的事或斷線，伺服器會當作視窗還開著而不再回應。
            raise ProtocolError("沒有收到寵物倉庫視窗（可能上一次沒有關閉倉庫視窗），已停止")
        self.objindex = found["window"].values[3]
        self.capacity, self.pets = parse_listing(found["window"].values[4])
        return self

    def _reply(self, data: bytes):
        world = self.session.world
        self._send(67, (world.x, world.y, 0, self.objindex, 0, data))

    def deposit(self, index: int):
        """把寵物欄第 index 格存進倉庫；騎寵、戰寵不存。"""
        world = self.session.world
        if self.objindex is None or index not in world.pets:
            raise ProtocolError("寵物倉庫未開啟或寵物欄沒有這隻寵物，已停止")
        if index in (world.mount_pet_index, world.battle_pet_index):
            raise ProtocolError("騎寵與戰寵不存倉庫，已停止")
        if len(self.pets) >= self.capacity:
            raise ProtocolError("寵物倉庫已滿，已停止")
        name = world.pets[index].name.decode("gbk", "replace")
        added = []

        def watch(event):
            if event.func == 808 and event.values[0] == WINDOW and event.values[4].startswith(b"Z|"):
                parts = event.values[4].decode("gbk", "replace").split("|")
                if len(parts) >= 9:
                    added.append(StoredPet(parts[1], int(parts[2]), parts[8]))

        self._reply(b"I|%d" % index)
        if not self._wait(lambda: index not in world.pets, watch=watch):
            raise ProtocolError(f"存入「{name}」沒有收到確認，已停止")
        self._wait(lambda: bool(added), 2.0, watch=watch)
        self.pets.extend(added)
        return name

    def withdraw(self, key: str):
        world = self.session.world
        if self.objindex is None or key not in [pet.key for pet in self.pets]:
            raise ProtocolError("寵物倉庫未開啟或沒有這個倉庫編號，已停止")
        if len(world.pets) >= 5:
            raise ProtocolError("寵物欄已滿，無法領出，已停止")
        before = set(world.pets)
        self._reply(b"B|" + key.encode("ascii"))
        if not self._wait(lambda: set(world.pets) - before):
            raise ProtocolError("領出寵物沒有收到確認，已停止")
        self.pets = [pet for pet in self.pets if pet.key != key]

    def close(self):
        if self.objindex is not None:
            self._reply(b"C|")
            self.objindex = None


class PersonalPetStorage:
    """寵物面板「倉庫」分頁的個人寵物倉庫（每隻角色自己的，不是共享倉庫）。

    2026-11-19 用真實客戶端操作並擷取封包：
    - 開啟：C821 (1, "O|") → S820 (1, "L|已存數|容量|100…")
    - 存入：C821 (1, "C|寵物欄索引|") → S820 (1, "Z|名稱|等級|血|地|水|火|風|倉庫編號|")
    - 查看：C821 (1, "X|倉庫編號|") → S820 (1, "I|倉庫編號|…")
    - 領出：C821 (1, "B|倉庫編號|") → S820 (1, "D|倉庫編號|")
    不必走到哪裡、也沒有關閉封包。沒等到伺服器確認就停，不重試。"""

    def __init__(self, session: Session):
        self.session, self.count, self.capacity = session, 0, 0

    def _exchange(self, data: bytes, prefix: bytes, seconds=6.0):
        session = self.session
        try:
            session._send_command(821, (1, data), time.monotonic() + session.timeout)
        except BaseException:
            session.close()
            raise
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is not None and event.func == 820 and event.values[0] == 1 and event.values[1].startswith(prefix):
                return event.values[1].decode("gbk", "replace").split("|")
        return None

    def open(self):
        if self.session.battle.active:
            raise ProtocolError("戰鬥中不可開寵物倉庫，已停止")
        self.session.sync()
        parts = self._exchange(b"O|", b"L|")
        try:
            self.count, self.capacity = int(parts[1]), int(parts[2])
            if not 0 <= self.count <= self.capacity <= 1000:
                raise ValueError
        except (TypeError, ValueError, IndexError):
            raise ProtocolError("沒有收到個人寵物倉庫清單或格式不符，已停止") from None
        return self

    def deposit(self, index: int) -> str:
        world = self.session.world
        if index not in world.pets or index in (world.mount_pet_index, world.battle_pet_index):
            raise ProtocolError("寵物欄沒有這隻寵物，或牠是騎寵／戰寵，不存倉庫，已停止")
        if self.count >= self.capacity:
            raise ProtocolError(f"個人寵物倉庫已滿（{self.capacity} 格），已停止")
        name = world.pets[index].name.decode("gbk", "replace")
        parts = self._exchange(b"C|%d|" % index, b"Z|")
        if parts is None or len(parts) < 9 or parts[1] != name:
            raise ProtocolError(f"存入「{name}」沒有收到確認，已停止")
        end = time.monotonic() + 3.0
        while index in world.pets and time.monotonic() < end:
            self.session.poll_event(0.3)
        world.pets.pop(index, None)          # 伺服器已確認存入；寵物欄的清空通知晚到也不影響
        self.count += 1
        return name


    def listing(self) -> list:
        """倉庫裡的寵物：[(名稱, 等級, 倉庫編號)…]。清單格式：`L|已存數|容量|100|` 之後每隻 8 欄
        （名稱、等級、血、地、水、火、風、倉庫編號；2026-11-21 實讀）。"""
        parts = self._exchange(b"O|", b"L|")
        if parts is None:
            raise ProtocolError("沒有收到個人寵物倉庫清單，已停止")
        try:
            return [(parts[index], int(parts[index + 1]), parts[index + 7]) for index in range(4, len(parts) - 7, 8)]
        except (ValueError, IndexError):
            raise ProtocolError("個人寵物倉庫清單格式不符，已停止") from None

    def withdraw(self, key: str) -> None:
        """領出一隻；寵物欄滿了或沒等到確認就停。"""
        world = self.session.world
        if len(world.pets) >= 5:
            raise ProtocolError("寵物欄滿了，不領出，已停止")
        before = set(world.pets)
        if self._exchange(f"B|{key}|".encode(), b"D|") is None:
            raise ProtocolError("領出寵物沒有收到確認，已停止")
        end = time.monotonic() + 4.0
        while time.monotonic() < end and set(world.pets) == before:
            self.session.poll_event(0.3)
        if set(world.pets) == before:
            raise ProtocolError("領出寵物後寵物欄沒有多一隻，已停止")


def withdraw_pet(session: Session, name: str, *, say=print) -> bool:
    """身上沒有叫 name 的寵物時，從個人寵物倉庫領一隻；個人倉庫沒有再找共享寵物倉庫。回傳有沒有領。
    轉生後要換騎寵時用（使用者 2026-11-21：轉生後騎老虎、放馬年）。兩邊都沒有就停。"""
    world = session.world
    if world.has_pet(name):
        return False
    personal = PersonalPetStorage(session)
    for stored, level, key in personal.listing():
        if stored == name:
            time.sleep(1.5)
            personal.withdraw(key)
            say(f"從個人寵物倉庫領出「{name}」（{level} 級）")
            return True
    shared = PetStorage(session).open()
    try:
        match = next((pet for pet in shared.pets if pet.name == name), None)
        if match is None:
            raise ProtocolError(f"個人與共享寵物倉庫都沒有「{name}」，已停止")
        time.sleep(1.5)
        shared.withdraw(match.key)
        say(f"從共享寵物倉庫領出「{name}」（{match.level} 級）")
    finally:
        if session.sock is not None:
            shared.close()
    return True


def store_spare_pets(session: Session, *, say=print, keep=()) -> list:
    """只留騎寵與戰寵，其餘寵物存進個人寵物倉庫（使用者 2026-11-19 指定：放寵物倉庫，不是共享倉庫）；
    回傳存入的名稱。keep 是任務要交出去的寵物名稱，不存。"""
    world = session.world
    if world.mount_pet_index is None or world.battle_pet_index is None:
        raise ProtocolError("還不知道騎寵與戰寵是哪一隻，不存倉庫")
    spare = [index for index in sorted(world.pets) if index not in (world.mount_pet_index, world.battle_pet_index)
             and world.pets[index].name.decode("gbk", "replace") not in keep]
    if not spare:
        return []
    storage = PersonalPetStorage(session).open()
    stored = []
    for index in spare:
        time.sleep(1.0)
        stored.append(storage.deposit(index))
        say(f"寵物「{stored[-1]}」已存入個人寵物倉庫（{storage.count}／{storage.capacity}）")
    return stored


def store_spare_pets_shared(session: Session, *, say=print, keep=()) -> list:
    """（舊做法）其餘寵物存進共享寵物倉庫；回傳存入的名稱。"""
    world = session.world
    if world.mount_pet_index is None or world.battle_pet_index is None:
        raise ProtocolError("還不知道騎寵與戰寵是哪一隻，不存倉庫")
    spare = [index for index in sorted(world.pets) if index not in (world.mount_pet_index, world.battle_pet_index)
             and world.pets[index].name.decode("gbk", "replace") not in keep]
    if not spare:
        return []
    storage = PetStorage(session).open()
    stored = []
    try:
        for index in spare:
            time.sleep(1.0)
            stored.append(storage.deposit(index))
            say(f"寵物「{stored[-1]}」已存入共享寵物倉庫（{len(storage.pets)}／{storage.capacity}）")
    finally:
        if session.sock is not None:
            storage.close()
    return stored


ITEM_WINDOW = 1125


def store_items(session: Session, keep, *, say=print) -> list:
    """把背包裡用不到的道具存進個人道具倉庫（道具面板的「倉庫」分頁，每隻角色自己的 30 格）。

    2026-11-19 用真實客戶端操作並擷取封包（和個人寵物倉庫同一組封包，種類改成 2）：
    - 開啟：C821 (2, "O|") → S820 (2, "L|已存數|容量|…")
    - 存入：C821 (2, "C|背包格|") → S820 (2, "Z|…|倉庫編號|…")
    - 領出：C821 (2, "B|倉庫編號|") → S820 (2, "D|倉庫編號|")
    綁定的任務道具伺服器不收（「特殊道具无法存入仓库」，2026-11-19 採伐許可證實測；綁定的智慧果倒是能存），
    所以綁定道具一律跳過。
    keep 是要留在身上的道具名稱；智慧果與肉不存（肉由清背包處理）。每存一格都等伺服器確認，沒等到就停。"""
    world = session.world
    if world.items is None:
        raise ProtocolError("沒有收到背包資料，已停止")
    spare = [slot for slot in sorted(world.items) if 9 <= slot <= 23 and world.items[slot][0] not in keep
             and slot not in world.bound
             and "智慧果" not in world.items[slot][0] and not world.items[slot][0].endswith("肉")]
    if not spare or session.battle.active:
        return []
    session.sync()

    def exchange(data: bytes, prefix: bytes, seconds=6.0):
        try:
            session._send_command(821, (2, data), time.monotonic() + session.timeout)
        except BaseException:
            session.close()
            raise
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is not None and event.func == 820 and event.values[0] == 2 and event.values[1].startswith(prefix):
                return event.values[1].split(b"|")
        return None

    listing = exchange(b"O|", b"L|")
    try:
        used, capacity = int(listing[1]), int(listing[2])
    except (TypeError, ValueError, IndexError):
        raise ProtocolError("沒有收到個人道具倉庫清單或格式不符，已停止") from None
    stored = []
    for slot in spare:
        if used + len(stored) >= capacity:
            say(f"個人道具倉庫滿了（{capacity} 格），剩下的道具留在身上")
            break
        name = world.items[slot][0]
        time.sleep(2.0)      # 連續存太快伺服器會不理（2026-11-19 四號第二件沒回應，隔開後正常）
        if exchange(b"C|%d|" % slot, b"Z|") is None:
            raise ProtocolError(f"存入「{name}」沒有收到確認，已停止")
        end = time.monotonic() + 3.0
        while slot in world.items and time.monotonic() < end:
            session.poll_event(0.3)
        if slot in world.items:
            raise ProtocolError(f"存入「{name}」後背包那一格沒有清空，已停止")
        stored.append(name)
    if stored:
        say(f"道具存入個人道具倉庫（{used + len(stored)}／{capacity}）：" + "、".join(stored))
    return stored


def store_named(session: Session, names, *, say=print) -> list:
    """把背包裡指定名稱的道具存進個人道具倉庫（使用者 2026-11-21：紅暴任務完成後，英雄的祝福存個倉）。
    和 store_items 不同：只存點名的這幾樣，綁定的也照樣試（store_items 一律跳過綁定道具）。
    伺服器不收（沒回確認）就記一行、留在身上，不算錯；倉庫滿了也只是記一行。回傳存進去的名稱。"""
    world = session.world
    if world.items is None or session.battle.active:
        return []
    slots = [slot for slot in sorted(world.items) if 9 <= slot <= 23 and world.items[slot][0] in names]
    if not slots:
        return []
    session.sync()

    def exchange(data: bytes, prefix: bytes, seconds=6.0):
        try:
            session._send_command(821, (2, data), time.monotonic() + session.timeout)
        except BaseException:
            session.close()
            raise
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is not None and event.func == 820 and event.values[0] == 2 and event.values[1].startswith(prefix):
                return event.values[1].split(b"|")
        return None

    listing = exchange(b"O|", b"L|")
    try:
        used, capacity = int(listing[1]), int(listing[2])
    except (TypeError, ValueError, IndexError):
        raise ProtocolError("沒有收到個人道具倉庫清單或格式不符，已停止") from None
    stored = []
    for slot in slots:
        name = world.items[slot][0]
        if used + len(stored) >= capacity:
            say(f"個人道具倉庫滿了（{capacity} 格），「{name}」留在身上")
            break
        time.sleep(2.0)
        if exchange(b"C|%d|" % slot, b"Z|") is None:
            say(f"「{name}」伺服器不收進個人道具倉庫，留在身上")
            continue
        end = time.monotonic() + 3.0
        while slot in world.items and time.monotonic() < end:
            session.poll_event(0.3)
        stored.append(name)
    if stored:
        say(f"存入個人道具倉庫（{used + len(stored)}／{capacity}）：" + "、".join(stored))
    return stored


def withdraw_items(session: Session, names, *, say=print) -> list:
    """把個人道具倉庫裡指定名稱的道具領到背包（已經在背包的不領）；回傳領出的名稱。
    轉生要把四大洞窟的證明帶在身上（使用者 2026-11-20）。封包同 store_items：開啟 `O|`、查內容 `X|編號|`、領出 `B|編號|`。
    倉庫裡找不到的不算錯（由呼叫端檢查背包齊不齊）；領出沒等到確認、或背包沒空位就停。"""
    world = session.world
    if world.items is None:
        raise ProtocolError("沒有收到背包資料，已停止")
    held = {world.items[slot][0] for slot in world.items}
    wanted = [name for name in names if name not in held]
    if not wanted or session.battle.active:
        return []
    session.sync()

    def exchange(data: bytes, prefix: bytes, seconds=6.0):
        try:
            session._send_command(821, (2, data), time.monotonic() + session.timeout)
        except BaseException:
            session.close()
            raise
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is not None and event.func == 820 and event.values[0] == 2 and event.values[1].startswith(prefix):
                return event.values[1].decode("gbk", "replace").split("|")
        return None

    listing = exchange(b"O|", b"L|")
    if listing is None:
        raise ProtocolError("沒有收到個人道具倉庫清單，已停止")
    taken = []
    for key in [listing[index + 3] for index in range(4, len(listing) - 5, 6)]:
        if not wanted:
            break
        time.sleep(1.5)
        info = exchange(f"X|{key}|".encode(), b"I|")
        if info is None or len(info) <= 2 or info[2] not in wanted:
            continue
        if world.free_bag_slots() < 1:
            raise ProtocolError(f"背包沒有空位領出「{info[2]}」，已停止")
        time.sleep(1.5)
        if exchange(f"B|{key}|".encode(), b"D|") is None:
            raise ProtocolError(f"領出「{info[2]}」沒有收到確認，已停止")
        end = time.monotonic() + 3.0
        while time.monotonic() < end and not world.has_item(info[2]):
            session.poll_event(0.3)
        if not world.has_item(info[2]):
            raise ProtocolError(f"領出「{info[2]}」後背包裡沒有看到，已停止")
        wanted.remove(info[2])
        taken.append(info[2])
    if taken:
        say("從個人道具倉庫領出：" + "、".join(taken))
    return taken


def store_items_shared(session: Session, keep, *, say=print) -> list:
    """（舊做法）把背包裡用不到的道具存進共享倉庫（C202 35／視窗 1125，回 `I|背包格` 存入、`C|` 關閉）。

    keep 是要留在身上的道具名稱（目前任務會用到的）；智慧果與肉不存（肉由清背包處理）。
    任務獎勵累積起來會把 15 格背包塞滿（2026-11-18 做到第 9 個任務時發生），所以每個任務開始前整理一次。
    每存一格都等伺服器回報該格清空；沒等到就停。回傳存入的名稱。"""
    world = session.world
    if world.items is None:
        raise ProtocolError("沒有收到背包資料，已停止")
    # 綁定道具伺服器不收（「特殊道具无法存入公共仓库」），先跳過。
    spare = [slot for slot in sorted(world.items) if 9 <= slot <= 23 and world.items[slot][0] not in keep
             and slot not in world.bound
             and "智慧果" not in world.items[slot][0] and not world.items[slot][0].endswith("肉")]
    if not spare or session.battle.active:
        return []
    session.sync()
    found = {}
    try:
        session._send_command(202, (35,), time.monotonic() + session.timeout)
    except BaseException:
        session.close()
        raise
    end = time.monotonic() + 6.0
    while "window" not in found and time.monotonic() < end:
        event = session.poll_event(0.3)
        if event is not None and event.func == 66 and event.values[0] == ITEM_WINDOW:
            found["window"] = event
    if "window" not in found:
        raise ProtocolError("沒有收到共享倉庫視窗（可能上一次沒有關閉），已停止")
    objindex, listing = found["window"].values[3], found["window"].values[4].split(b"|")
    try:
        used, capacity = int(listing[1]), int(listing[2])
    except (ValueError, IndexError):
        raise ProtocolError("共享倉庫清單格式不符，已停止") from None
    stored = []

    def reply(data: bytes):
        try:
            session._send_command(67, (world.x, world.y, 0, objindex, 0, data), time.monotonic() + session.timeout)
        except BaseException:
            session.close()
            raise

    try:
        for slot in spare:
            if used + len(stored) >= capacity:
                say(f"共享倉庫滿了（{capacity} 格），剩下的道具留在身上")
                break
            name = world.items[slot][0]
            time.sleep(0.8)
            reply(b"I|%d" % slot)
            end = time.monotonic() + 5.0
            while slot in world.items and time.monotonic() < end:
                session.poll_event(0.3)
            if slot in world.items:
                raise ProtocolError(f"存入「{name}」沒有收到確認，已停止")
            stored.append(name)
    finally:
        if session.sock is not None:
            reply(b"C|")
    if stored:
        say(f"道具存入共享倉庫（{used + len(stored)}／{capacity}）：" + "、".join(stored))
    return stored
