"""寵物檔次查詢，以及把寵物賣給瑪麗娜絲漁村寵物店的「宠物回收员」。

規則（使用者 2026-11-24）：寵物練滿 140 級之後，老虎、雷龍只要檔次沒有滿檔，都可以賣給漁村寵物店。
「滿檔」＝四圍檔次（體力、腕力、耐力、速度）每一項都等於同種的極品值。

封包都來自 dkbo/遊戲客戶端-AI doc/PACKETS.md 的官方錄檔：
- 檔次查詢（§25.4）：C821 (30, "角色序號|寵物欄") → S66，內文列出四項「你的／极品值」。
- 賣寵物（§33，2026-11-23 成交一筆 140 級貝魯卡 197399 石幣）：漁村 2000 的 (75,58) 進寵物店 2003，
  站 (13,12) 朝 6 對宠物回收员 (12,12) 說話 → S66 類型 111 → C67 回 "1"（卖出宠物）→ S66 類型 3 →
  C67 回「寵物欄＋1」→ S66 確認視窗「您确定要卖出[名稱]吗?卖出价格:N石币」→ C67 按確定（選擇 1、資料空）→ 訊息「获得石币N」。
- 0 轉 50 級以下的角色不能賣（伺服器回「0转50级以下无法贩卖宠物」）。

賣寵物不可逆，所以每一步都核對：確認視窗上的名稱要和要賣的那一隻相同、價格要大於 0，不對就不按確定、直接停。
出錯就停，不重試。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .session import ProtocolError, Session

MAX_PET_LEVEL = 140
TIGERS = ("贝鲁卡", "贝鲁伊卡", "格鲁西斯", "金格萨贝鲁")
THUNDERS = ("布洛多斯", "布林帖斯", "邦恩多斯", "布拉奇多斯", "斯天多斯")
SELLABLE = TIGERS + THUNDERS             # 滿級後檔次沒滿就可以賣的種類
VILLAGE_MAP, SHOP_MAP, RECORD_VILLAGE = 2000, 2003, 2006
SHOP_DOOR = (75, 58)
RECYCLER = ((13, 12), 6)                 # 站的位置、說話方向（宠物回收员在 (12,12)）
GRADE_LINE = re.compile(r"(体力|腕力|耐力|速度)：(?:\[/style\])?\s*(\d+)\s+极品值：\s*(\d+)")
CONFIRM = re.compile(r"卖出\[(.+?)\]吗\?卖出价格:(\d+)石币")


def parse_grade(text: str) -> list:
    """檔次視窗內文 → [(項目, 你的, 極品值)…]（四項）；格式不對回空的。"""
    rows = [(name, int(mine), int(best)) for name, mine, best in GRADE_LINE.findall(text)]
    return rows if len(rows) == 4 else []


def grade_diff(rows: list) -> int:
    """總檔差：每項＝你的 −（極品值 − 2），四項相加，−8～+8。"""
    return sum(mine - (best - 2) for _, mine, best in rows)


def is_full(rows: list) -> bool:
    return bool(rows) and all(mine >= best for _, mine, best in rows)


def query_grade(session: Session, sub: int, slot: int) -> list:
    """查寵物欄 slot 那一隻的四圍檔次；沒有收到視窗回空的（不重送）。"""
    session._send_command(821, (30, f"{sub}|{slot}".encode()), time.monotonic() + session.timeout)
    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        event = session.poll_event(0.2)
        if event is not None and event.func == 66 and isinstance(event.values[4], bytes):
            text = event.values[4].decode("gbk", "replace")
            if "档次" in text:
                return parse_grade(text)
    return []


def should_sell(name: str, level: int, rows: list) -> bool:
    """照使用者的規則：滿 140 級的老虎、雷龍，檔次查得到而且沒有滿檔，才賣。查不到檔次一律不賣。"""
    return name in SELLABLE and level >= MAX_PET_LEVEL and bool(rows) and not is_full(rows)


def go_to_shop(session: Session, map_dir: Path) -> None:
    from . import quest
    world = session.world
    if world.map_id == SHOP_MAP:
        return
    if world.map_id != VILLAGE_MAP:
        if world.map_id not in (quest.RECORD_MAP, RECORD_VILLAGE):
            session.return_to_record()
            time.sleep(2.0)
        session.teleport(quest.teleport_code(VILLAGE_MAP))
    if world.map_id != VILLAGE_MAP:
        raise ProtocolError(f"到不了瑪麗娜絲漁村（停在地圖 {world.map_id}），已停止")
    quest.walk(session, map_dir, SHOP_DOOR, say=lambda message: None)
    quest.enter_warp(session, map_dir, SHOP_DOOR, say=lambda message: None)
    if world.map_id != SHOP_MAP:
        raise ProtocolError(f"進不了漁村寵物店（停在地圖 {world.map_id}），已停止")


def sell(session: Session, map_dir: Path, slot: int, *, say=print) -> int:
    """把寵物欄 slot 那一隻賣給宠物回收员；回傳賣得的石幣。騎寵、戰寵不賣。"""
    from . import quest
    world = session.world
    if slot not in world.pets or slot in (world.mount_pet_index, world.battle_pet_index):
        raise ProtocolError("寵物欄沒有這隻寵物，或牠是騎寵／戰寵，不賣，已停止")
    name = world.pets[slot].name.decode("gbk", "replace")
    go_to_shop(session, map_dir)
    spot, facing = RECYCLER
    if (world.x, world.y) != spot:
        quest.walk(session, map_dir, spot, say=lambda message: None)
    if (world.x, world.y) != spot:
        raise ProtocolError(f"走不到宠物回收员前面 {spot}，已停止")
    notes = []

    def window(seconds: float = 6.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is None:
                continue
            if event.func == 263 and isinstance(event.values[0], bytes):
                notes.append(event.values[0].decode("gbk", "replace"))
            if event.func == 66:
                return event
        return None

    def reply(found, choice: int, data: bytes):
        time.sleep(1.0)
        session._send_command(67, (world.x, world.y, found.values[2], found.values[3], choice, data),
                              time.monotonic() + session.timeout)

    session.sync()
    session._send_command(34, (facing,), time.monotonic() + session.timeout)
    menu = window()
    if menu is None or menu.values[0] != 111:
        raise ProtocolError("宠物回收员沒有開出選單，已停止")
    reply(menu, 0, b"1")                                  # 卖出宠物
    chooser = window()
    if chooser is None or chooser.values[0] != 3:
        raise ProtocolError(f"沒有出現選寵物的視窗（訊息：{notes}），已停止")
    reply(chooser, 0, str(slot + 1).encode())             # 寵物欄＋1
    confirm = window()
    text = confirm.values[4].decode("gbk", "replace") if confirm is not None and isinstance(confirm.values[4], bytes) else ""
    found = CONFIRM.search(text)
    if found is None:
        raise ProtocolError(f"沒有出現賣出確認視窗（訊息：{notes or text[:60]}），沒有賣，已停止")
    if found.group(1) != name or int(found.group(2)) <= 0:
        raise ProtocolError(f"確認視窗寫的是「{found.group(1)}」{found.group(2)} 石幣，和要賣的「{name}」對不上，不按確定，已停止")
    reply(confirm, 1, b"")                                # 確定
    end = time.monotonic() + 6.0
    while time.monotonic() < end and slot in world.pets and not any("获得石币" in note for note in notes):
        event = session.poll_event(0.3)
        if event is not None and event.func == 263 and isinstance(event.values[0], bytes):
            notes.append(event.values[0].decode("gbk", "replace"))
    if not any("获得石币" in note for note in notes):
        raise ProtocolError(f"按了確定但沒有收到賣出的訊息（訊息：{notes}），已停止")
    world.pets.pop(slot, None)
    say(f"把{name}賣給漁村寵物店，得到 {found.group(2)} 石幣")
    return int(found.group(2))
