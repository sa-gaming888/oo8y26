"""面板資料：把連線收到的狀態訊息整理成人物、寵物、道具、任務四個面板，存成快照檔給查詢 API 用。

資料來源是伺服器在登入與變動時推送的狀態訊息（`world.raw` 保留的完整原文），沒有另外向伺服器發查詢：
- 人物 `P1`（56 欄）、寵物 `K0`～`K4`（58 欄）與技能 `W`、道具 `I`（54 格 × 23 欄）、任務進度 S809。
- 道具與任務只有「自己登入的那條連線」收得到；隊長連線上只收得到隊員的人物與寵物。
  所以完整快照要每隻角色單獨登入一次（練功每輪開頭的收尾、或 `--refresh`）。

欄位對應見 doc/UI-PROTOCOL.md；還沒確認含意的欄位放在「未確認欄位」，原樣給出。

    python -X utf8 -m oo8y26.panels --refresh            # 五隻各自登入一次，更新快照（練功或任務進行中不可用）
    python -X utf8 -m oo8y26.panels --show 1             # 印出快照裡一號的面板
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

SNAPSHOT_FILE = Path(__file__).resolve().parent.parent / "captures" / "panels.local.json"
ACTIVITY_FILE = Path(__file__).resolve().parent.parent / "captures" / "activity.local.json"
ACTIVITY_STALE = 900.0       # 活動標記超過這麼久沒更新就當作已經沒在跑

# P1 各欄的名稱（欄位編號 → 名稱）。標「推測」的見 doc/UI-PROTOCOL.md。
CHARACTER_FIELDS = {
    1: "生命", 2: "最大生命", 3: "氣", 4: "最大氣", 5: "體力", 6: "腕力", 7: "耐力", 8: "速度",
    9: "經驗", 10: "升級所需經驗", 11: "等級", 12: "攻擊", 13: "防禦", 14: "敏捷", 15: "魅力", 16: "運氣",
    17: "地", 18: "水", 19: "火", 20: "風", 21: "石幣", 22: "經驗加成剩餘秒", 24: "轉生數",
}
PET_FIELDS = {
    3: "生命", 4: "最大生命", 7: "經驗", 8: "升級所需經驗", 9: "等級", 10: "攻擊", 11: "防禦", 12: "敏捷",
    13: "忠誠", 14: "地", 15: "水", 16: "火", 17: "風", 20: "轉生數",
}
EQUIP_SLOTS = 9              # 道具欄第 0～8 格是裝備，9 起是背包


def _text(value) -> str:
    return value.decode("gbk", "replace") if isinstance(value, (bytes, bytearray)) else str(value)


def _number(value):
    text = _text(value)
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def character_panel(world) -> dict | None:
    parts = world.raw.get("P1")
    if parts is None:
        return None
    panel = {"名稱": _text(parts[29]), "稱號": _text(parts[30])}
    panel.update({name: _number(parts[index]) for index, name in CHARACTER_FIELDS.items()})
    money = world.raw.get("貨幣")
    if money is not None and len(money) >= 3:
        # S262：第 0 個石幣、第 2 個聲望對過畫面；其餘（金幣、水晶、彩鑽、寶石、活力、戰點之類）還沒對出來，原樣附上。
        panel["聲望"] = money[2]
        panel["貨幣原文"] = list(money)
    panel["騎寵欄位"] = int(parts[25])
    panel["戰寵欄位"] = world.battle_pet_index
    panel["地圖"] = world.map_id
    panel["座標"] = [world.x, world.y]
    known = set(CHARACTER_FIELDS) | {0, 25, 29, 30}
    panel["未確認欄位"] = {str(index): _text(value) for index, value in enumerate(parts)
                      if index not in known and value not in (b"", b"0")}
    return panel


def pets_panel(world) -> list:
    pets = []
    for index in range(5):
        parts = world.raw.get(f"K{index}")
        if parts is None:
            continue
        pet = {"欄位": index, "名稱": _text(parts[22]), "自訂名": _text(parts[23])}
        pet.update({name: _number(parts[field]) for field, name in PET_FIELDS.items()})
        pet["騎乘"] = index == world.mount_pet_index
        pet["出戰"] = index == world.battle_pet_index
        if len(parts) >= 53:
            pet["成長率"] = [_number(value) for value in parts[49:53]]
        pet["技能"] = [name for name in world.pet_skills.get(index, []) if name]
        pets.append(pet)
    return pets


def items_panel(world) -> dict | None:
    parts = world.raw.get("I")
    if parts is None:
        return None
    equipment, bag = [], []
    for slot in range(len(parts) // 23):
        row = parts[slot * 23:slot * 23 + 23]
        if not row[0]:
            continue
        item = {"格": slot, "名稱": _text(row[0]), "數量": _number(row[10]) or 1, "類別": _text(row[11]),
                "狀態": _text(row[9]), "說明": _text(row[3])}
        (equipment if slot < EQUIP_SLOTS else bag).append(item)
    free = sum(1 for slot in range(9, 24) if not parts[slot * 23])
    return {"裝備": equipment, "背包": bag, "背包空格": free}


def quests_panel(world) -> list | None:
    if world.tasks is None:
        return None
    from . import quest
    progress = quest.parse_progress(*world.tasks)
    rows = []
    try:
        catalog = quest.load_tasks().values()
    except (OSError, ValueError, KeyError):
        # 沒有任務清單檔（公開版容器不帶）：只給伺服器回報的進度，沒有名稱與總步數。
        # 2026-11-20 容器因為缺這個檔在第一次存快照時停掉，所以這裡不能讓整個練功跟著停。
        numbers = sorted(set(progress.done) | set(progress.steps))
        return [{"編號": number, "狀態": "已完成" if number in progress.done else "進行中",
                 "做完步數": None if number in progress.done else progress.steps[number]} for number in numbers]
    for task in catalog:
        number, total = task["id"], len(task["steps"])
        if number in progress.done:
            state, done = "已完成", total
        elif number in progress.steps:
            state, done = "進行中", progress.steps[number]
        else:
            state, done = "未接", 0
        rows.append({"編號": number, "名稱": task["title"], "狀態": state, "做完步數": done, "總步數": total})
    return rows


def snapshot_of(world) -> dict:
    """一條連線目前看得到的面板；收不到的面板不放（不會用空的蓋掉舊資料）。"""
    result = {}
    for name, panel in (("人物", character_panel(world)), ("寵物", pets_panel(world) if world.raw.get("P1") else None),
                        ("道具", items_panel(world)), ("任務", quests_panel(world))):
        if panel is not None:
            result[name] = panel
    return result


def fetch_currency(session, seconds: float = 4.0) -> bool:
    """開一次商城視窗讓伺服器送貨幣資料（聲望只有這裡有）；只看、不換分頁、不買。戰鬥中或沒收到就算了，回 False。"""
    if session.battle.active:
        return False
    session.world.raw.pop("貨幣", None)
    session._send_command(202, (31,), time.monotonic() + session.timeout)
    end = time.monotonic() + seconds
    while time.monotonic() < end and "貨幣" not in session.world.raw:
        session.poll_event(0.3)
    return "貨幣" in session.world.raw


def load(path: Path = None) -> dict:
    try:
        data = json.loads((path or SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"角色": {}}
    return data if isinstance(data.get("角色"), dict) else {"角色": {}}


def record(sub: int, world, path: Path = None, now=None) -> dict:
    """把這條連線看得到的面板併進快照檔（每個面板各記更新時間）。寫檔失敗不影響呼叫端。"""
    path = path or SNAPSHOT_FILE
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    fresh = snapshot_of(world)
    if not fresh:
        return {}
    data = load(path)
    entry = data["角色"].setdefault(str(sub), {})
    times = entry.setdefault("更新時間", {})
    for name, panel in fresh.items():
        entry[name] = panel
        times[name] = stamp
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass
    return fresh


def record_team(world, path: Path = None) -> None:
    """隊長連線上：記下自己的全部面板，以及隊員的人物與寵物（隊員的道具、任務這條連線收不到）。"""
    leader = world.roles.controlled_slot or 1
    record(leader, world, path)
    for slot, companion in world.companions.items():
        if slot != leader and companion.raw.get("P1"):
            record(slot, companion, path)


def mark_activity(name: str, path: Path = None) -> None:
    """練功／任務進行中的標記（定時更新）；查詢 API 靠它知道現在不能另外登入。"""
    try:
        path = path or ACTIVITY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"活動": name, "時間": time.time()}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class activity:
    """with 區塊：進行中每 60 秒更新一次活動標記，結束時清掉。"""

    def __init__(self, name: str, path: Path = None):
        self.name, self.path = name, path
        self._stop = None

    def __enter__(self):
        import threading
        self._stop = threading.Event()
        mark_activity(self.name, self.path)

        def beat():
            while not self._stop.wait(60.0):
                mark_activity(self.name, self.path)
        threading.Thread(target=beat, daemon=True).start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        clear_activity(self.path)
        return False


def clear_activity(path: Path = None) -> None:
    try:
        (path or ACTIVITY_FILE).unlink()
    except OSError:
        pass


def running_activity(path: Path = None, now=None):
    """正在跑的活動名稱；沒有（或標記太舊）回 None。"""
    try:
        data = json.loads((path or ACTIVITY_FILE).read_text(encoding="utf-8"))
        if (now or time.time()) - float(data["時間"]) < ACTIVITY_STALE:
            return str(data["活動"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def refresh(config: dict, subs=range(1, 6), path: Path = None, say=print) -> list:
    """每隻角色單獨登入一次，更新完整快照。練功或任務進行中不做（單獨登入會把隊員拉離隊伍）。"""
    activity = running_activity()
    if activity is not None:
        raise RuntimeError(f"「{activity}」進行中，不能另外登入更新；快照會由它每輪自動更新")
    from .modules import connect
    done = []
    for sub in subs:
        with connect(config, sub) as session:
            fetch_currency(session)
            record(sub, session.world, path)
        done.append(sub)
        say(f"角色 {sub} 的面板已更新")
        time.sleep(2.0)
    return done


def summary(data: dict) -> list:
    rows = []
    for sub in sorted(data["角色"], key=int):
        entry = data["角色"][sub]
        person = entry.get("人物", {})
        rows.append({"角色": int(sub), "名稱": person.get("名稱"), "等級": person.get("等級"),
                     "轉生數": person.get("轉生數"), "經驗": person.get("經驗"),
                     "升級所需經驗": person.get("升級所需經驗"), "石幣": person.get("石幣"),
                     "聲望": person.get("聲望"), "生命": person.get("生命"), "最大生命": person.get("最大生命"),
                     "寵物": [f"{pet['名稱']} Lv{pet['等級']}" for pet in entry.get("寵物", [])],
                     "背包空格": entry.get("道具", {}).get("背包空格"),
                     "任務已完成": sum(1 for row in entry.get("任務", []) if row["狀態"] == "已完成") or None,
                     "更新時間": entry.get("更新時間", {})})
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("config/query.local.json"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--show", type=int, choices=range(1, 6))
    args = parser.parse_args(argv)
    try:
        if args.refresh:
            config = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if config.get("test_only") is not True:
                raise ValueError("只接受測試帳號設定")
            from .modules import load_profiles
            line = load_profiles().get("線路", {})
            config["port"] = line.get("port", config["port"])
            config["server_index"] = line.get("server_index", config["server_index"])
            refresh(config)
        data = load()
        shown = data["角色"].get(str(args.show)) if args.show else summary(data)
        print(json.dumps(shown, ensure_ascii=False, indent=1))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f"停止：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
