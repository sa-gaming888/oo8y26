"""商城的石幣分頁：用遊戲內的石幣買任務道具。金幣、彩鑽、寶石等付費貨幣的分頁一律不碰。

2026-11-19 對照客戶端 `lua/win/1017.xy` 並線上實測（港澳一線）：
- 開啟：C202(31) → S66 (1017, 8, 0, objindex, "目前分頁|分頁位元|數量|名稱|圖號|價格|說明…", "金币商城", …)，預設是分頁 1（金幣）
- 換分頁：C67 (x, y, 0, objindex, 0, "T|分頁|") → S808 (1017, …, 同樣格式的清單)；分頁 2 是石幣
- 購買：C67 (…, "B|分頁|第幾項（從 1 起）|數量")；買到的道具照常由 S22 推進背包
- 關閉：客戶端不送任何封包
分頁 2 以外的購買指令這個模組不會送；價格超過上限、背包沒位置、沒等到道具進背包都直接停。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .session import ProtocolError, Session

WINDOW = 1017
MENU = 31
STONE = 2                 # 石幣分頁；其餘分頁是付費貨幣
MAX_PRICE = 1000          # 單件石幣價格上限（任務道具實測是 0）
MAX_PURCHASES = 8         # 一次最多買幾樣


@dataclass(frozen=True)
class Goods:
    index: int            # 購買時用的項次（從 1 起）
    name: str
    price: int


def parse_listing(message: bytes):
    """解析 `分頁|分頁位元|數量|…每項 4 欄（名稱、圖號、價格、說明）`，回傳 (分頁, [Goods…])。"""
    try:
        parts = message.decode("gbk").split("|")
        kind, count = int(parts[0]), int(parts[2])
        if not 0 <= count <= 500 or len(parts) < 3 + count * 4:
            raise ValueError
        goods = [Goods(i + 1, parts[3 + i * 4], int(parts[5 + i * 4])) for i in range(count)]
        if any(not item.name or item.price < 0 for item in goods):
            raise ValueError
        return kind, goods
    except (ValueError, IndexError, UnicodeError):
        raise ProtocolError("商城清單格式不符，已停止") from None


def buy_with_stone(session: Session, names, *, say=print, maximum_price: int = MAX_PRICE) -> list:
    """到商城石幣分頁把 names 裡身上還沒有的道具各買一個；回傳買到的名稱。"""
    world = session.world
    wanted = [name for name in dict.fromkeys(names) if not world.has_item(name)]
    if not wanted:
        return []
    if len(wanted) > MAX_PURCHASES:
        raise ProtocolError("一次要買的道具太多，已停止")
    if session.battle.active:
        raise ProtocolError("戰鬥中不可開商城，已停止")
    if world.free_bag_slots() < len(wanted):
        raise ProtocolError(f"背包空位不夠放 {len(wanted)} 樣道具，已停止")
    try:
        session.sync()

        def wait(func, seconds=6.0):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                event = session.poll_event(0.3)
                if event is not None and event.func == func and event.values[0] == WINDOW:
                    return event
            return None

        session._send_command(202, (MENU,), time.monotonic() + session.timeout)
        window = wait(66)
        if window is None:
            raise ProtocolError("沒有收到商城視窗，已停止")
        objindex = window.values[3]

        def reply(data: bytes):
            session._send_command(67, (world.x, world.y, 0, objindex, 0, data), time.monotonic() + session.timeout)

        kind, goods = parse_listing(window.values[4])
        if kind != STONE:
            time.sleep(0.8)
            reply(b"T|%d|" % STONE)
            page = wait(808)
            if page is None:
                raise ProtocolError("換到石幣分頁沒有回應，已停止")
            kind, goods = parse_listing(page.values[4])
        if kind != STONE:
            raise ProtocolError("商城沒有切到石幣分頁，不購買，已停止")
        bought = []
        for name in wanted:
            item = next((entry for entry in goods if entry.name == name), None)
            if item is None:
                raise ProtocolError(f"商城石幣分頁沒有「{name}」，已停止")
            if item.price > maximum_price:
                raise ProtocolError(f"「{name}」要 {item.price} 石幣，超過上限 {maximum_price}，不購買，已停止")
            time.sleep(1.0)
            reply(b"B|%d|%d|1" % (STONE, item.index))
            end = time.monotonic() + 6.0
            while not world.has_item(name) and time.monotonic() < end:
                session.poll_event(0.3)
            if not world.has_item(name):
                raise ProtocolError(f"買「{name}」後道具沒有進背包，已停止")
            bought.append(name)
            say(f"商城石幣分頁買到「{name}」（{item.price} 石幣）")
        return bought
    except BaseException:
        session.close()
        raise
