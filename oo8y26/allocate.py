"""升級點數配點：子號單獨登入後，把剩餘點數依比例配到體力／腕力／耐力／敏捷。

實測（2026-11-17）：
- 伺服器在登入時以 S63 推送剩餘點數；每升一級 3 點。
- 配一點＝ C64(屬性編號)，編號 0 體力、1 腕力、2 耐力、3 敏捷；伺服器回 S63(新的剩餘點數) 與人物狀態差異。
- 只對「目前登入的角色」有效。五控的 C2014(槽位, 值) 雖然回 S2019(…, 1)，但**不是**配點，不要用。
- C62 是字串欄位的指令（自訂稱號），不是配點。

每送一點都等 S63 確認遞減才送下一點；沒等到就停，不重試。

    python -m oo8y26.allocate --config config/query.local.json --sub 2 --ratio 10,9,0,5
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .session import ProtocolError, Session

ATTRIBUTES = ("體力", "腕力", "耐力", "敏捷")
MAX_POINTS = 2000          # 單次執行的上限保護


def plan_points(points: int, ratio: tuple) -> list[int]:
    """把 points 點依 ratio（四個非負整數）分配，回傳要送出的屬性編號序列（平均交錯）。"""
    if len(ratio) != 4 or any(not isinstance(r, int) or r < 0 for r in ratio) or sum(ratio) == 0:
        raise ValueError("比例必須是四個非負整數且不可全為 0")
    if not 0 <= points <= MAX_POINTS:
        raise ValueError("點數超出上限")
    total = sum(ratio)
    given = [0, 0, 0, 0]
    order = []
    for sent in range(1, points + 1):
        # 每一步選「應得點數與已得點數差距最大」的屬性，結果會貼近比例且前後平均。
        index = max(range(4), key=lambda i: (ratio[i] * sent / total - given[i], -i))
        given[index] += 1
        order.append(index)
    return order


def allocate(config: dict, slot: int, ratio: tuple) -> dict:
    """登入指定子號並配完所有剩餘點數；回傳 {"points": 原有點數, "given": 各屬性配了幾點}。"""
    remaining = {"value": None}
    with Session(timeout=10) as session:
        session.authenticate(config["host"], int(config["port"]), config["account"], str(slot),
                             config["subaccounts"][str(slot)]["password_md5"], server_index=int(config["server_index"]))
        session.request_characters()
        session.select_character()

        def pump(seconds, done):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                event = session.poll_event(0.2)
                if event is not None and event.func == 63:
                    remaining["value"] = event.values[0]
                if done():
                    return True
            return done()

        # S63 在選角後、進世界前後推送；進世界時由 _read_event 處理，這裡另外攔一次。
        original = session._read_event

        def read_event(deadline):
            event = original(deadline)
            if event.func == 63:
                remaining["value"] = event.values[0]
            return event

        session._read_event = read_event
        session.read_initial_world()
        session.sync()
        if not pump(3.0, lambda: remaining["value"] is not None):
            raise ProtocolError("沒有收到剩餘點數，已停止")
        points = remaining["value"]
        # 配點前先取消原地遇敵（使用者 2026-11-20）：免得配到一半被拉進戰鬥。沒在遇敵時送這個也沒有副作用。
        session._send_command(202, (18,), time.monotonic() + session.timeout)
        pump(0.6, lambda: False)
        if session.battle.active:
            raise ProtocolError("角色在戰鬥中，不配點")
        given = [0, 0, 0, 0]
        for index in plan_points(points, ratio):
            before = remaining["value"]
            session._send_command(64, (index,), time.monotonic() + session.timeout)
            if not pump(4.0, lambda: remaining["value"] == before - 1):
                raise ProtocolError(f"配點沒有收到確認（已配 {sum(given)} 點），已停止")
            given[index] += 1
        return {"points": points, "given": given, "hp": (session.world.hp, session.world.max_hp)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sub", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--ratio", default="10,9,0,5", help="體力,腕力,耐力,敏捷 的比例（預設 10,9,0,5）")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if config.get("test_only") is not True:
            raise ValueError("只接受測試帳號設定")
        ratio = tuple(int(part) for part in args.ratio.split(","))
        result = allocate(config, args.sub, ratio)
        detail = "、".join(f"{name} +{count}" for name, count in zip(ATTRIBUTES, result["given"]) if count)
        print(f"子號 {args.sub}：原有 {result['points']} 點，" + (detail or "沒有可配的點數")
              + f"；生命 {result['hp'][0]}/{result['hp'][1]}")
        return 0
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        print(f"子號 {args.sub}：配點停止：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
