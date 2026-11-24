"""每日在線獎勵（玩家口中的「簽到」）：活動面板的「在线奖励」分頁，每天四檔，各給一顆「五倍智慧果(1小时)」。

封包（dkbo/遊戲客戶端-AI doc/PACKETS.md §20.1 的官方錄檔，2026-11-18；本專案 2026-11-24 起照做）：
- 開活動面板：C202 (34) → S66 視窗 1100、物件 12141。第一次開伺服器回得很慢（錄檔約 4～10 秒）：只送一次、等久一點。
- 切到在線獎勵：C67 (x, y, 0, 物件, 0, "G|2|") → S808 `X|2|L|<今天在線秒數>|{已領旗標|名稱|圖號|數量|耐久|說明}×4`
- 領第 n 檔：C67 (x, y, 0, 物件, 2, "G|n|") → S808 `X|2|C|n|`，道具進背包。
- 四檔的門檻是在線 10 分鐘、30 分鐘、1 小時、2 小時（客戶端腳本的數字）；沒到門檻客戶端不送，這裡也不送。

只領這一個分頁；同一個面板的贊助、月卡、來吉卡、CDKEY 都是付費或兌換，不碰。
出錯就停：面板沒回、領了沒有確認都直接丟 ProtocolError，不重送。
"""
from __future__ import annotations

import time

from .session import ProtocolError, Session

MENU = 34
WINDOW = 1100
TAB = 2
THRESHOLDS = (600, 1800, 3600, 7200)       # 第 1～4 檔要在線幾秒
OPEN_SECONDS = 15.0                        # 第一次開面板伺服器回得慢


def parse_listing(message: bytes) -> tuple:
    """解析 `X|2|L|秒數|…每檔 6 欄`，回傳（在線秒數, [(已領, 名稱)…]）。"""
    parts = message.decode("gbk", "replace").split("|")
    try:
        if parts[:3] != ["X", str(TAB), "L"]:
            raise ValueError
        seconds = int(parts[3])
        tiers = [(parts[index] == "1", parts[index + 1]) for index in range(4, len(parts) - 5, 6)]
        if not tiers or len(tiers) > len(THRESHOLDS):
            raise ValueError
        return seconds, tiers
    except (ValueError, IndexError):
        raise ProtocolError("在線獎勵清單格式不符，已停止") from None


def claim_online_rewards(session: Session, *, say=print) -> list:
    """把今天已經到門檻、還沒領的在線獎勵領下來；回傳領到的道具名稱。背包沒空位就不領。"""
    world = session.world
    if session.battle.active:
        raise ProtocolError("戰鬥中不可開活動面板，已停止")

    def wait(func, prefix: bytes, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if (event is not None and event.func == func and event.values[0] == WINDOW
                    and (func == 66 or event.values[4].startswith(prefix))):
                return event
        return None

    try:
        session.sync()
        session._send_command(202, (MENU,), time.monotonic() + session.timeout)
        window = wait(66, b"", OPEN_SECONDS)
        if window is None:
            raise ProtocolError("沒有收到活動面板，已停止")
        objindex = window.values[3]

        def reply(choice: int, data: bytes):
            session._send_command(67, (world.x, world.y, 0, objindex, choice, data), time.monotonic() + session.timeout)

        time.sleep(1.0)
        reply(0, b"G|%d|" % TAB)
        page = wait(808, b"X|%d|L|" % TAB, OPEN_SECONDS)
        if page is None:
            raise ProtocolError("在線獎勵分頁沒有回應，已停止")
        seconds, tiers = parse_listing(page.values[4])
        claimed = []
        for number, (done, name) in enumerate(tiers, start=1):
            if done or seconds <= THRESHOLDS[number - 1]:
                continue
            if world.free_bag_slots() < 1:
                say(f"背包沒有空位，在線獎勵第 {number} 檔（{name}）先不領")
                break
            time.sleep(1.5)
            reply(TAB, b"G|%d|" % number)
            if wait(808, b"X|%d|C|%d|" % (TAB, number), 6.0) is None:
                raise ProtocolError(f"領在線獎勵第 {number} 檔沒有收到確認，已停止")
            claimed.append(name)
        waiting = [number for number, (done, _) in enumerate(tiers, start=1)
                   if not done and seconds <= THRESHOLDS[number - 1]]
        say(f"在線獎勵：今天在線 {seconds // 60} 分鐘，領到 {claimed or '無'}"
            + (f"，第 {waiting} 檔還沒到時間" if waiting else ""))
        end = time.monotonic() + 0.8          # 讓背包資料到齊
        while time.monotonic() < end:
            session.poll_event(0.2)
        return claimed
    except BaseException:
        session.close()
        raise
