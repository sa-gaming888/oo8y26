"""任務：解析伺服器的任務進度（S809），照客戶端的任務腳本走到 NPC 面前對話。

2026-11-18 以「送貝殼」（任務 2）實測整個流程：
- 進度來自 S809 `(完成位元, 步驟進度)`：完成位元是 32 個整數（任務編號 = 第幾個整數 × 32 + 位元），
  步驟進度是 `任務-已完成步驟|…`。接到任務後變成 `2-1|`，交完任務後該項消失、位元 2 亮起。
- 接任務／交任務都是「走到 NPC 旁 → C34(方向) → S66 視窗 → C67 回覆」，沒有專用的任務封包。
- 每一步怎麼走寫在客戶端 `data/script/<任務>-<步驟>.txt`（已解到 knowledge/quest_scripts）。

腳本指令裡，這裡只執行已經看懂的：`B`（貼心傳送代號）、`M`／`M1`（預期所在地圖）、`G x y`（走到該格，
下一行是別張地圖時代表這格是換圖點）、`npc对话 方向`、`I 道具 行號`（沒有道具就跳到該行）、`S`（提示）、`END`。
遇到其他指令（`T`、`F`、`P`…）就停，不亂猜。遇敵也停。

    python -m oo8y26.quest --config config/query.local.json --map-dir <client>/map --task 5
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .session import ProtocolError, Session

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "quest_scripts"
TASKS_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "tasks.json"
RECORD_MAP = 1006
# 腳本的 B 參數：小於 1000 的就是貼心傳送代號；村莊用地圖編號寫，要換成代號（只列實測過的）。
VILLAGE_CODES = {2000: 110, 1000: 109, 3000: 111, 4000: 112}   # 4000→112（卡魯他那）是照順序推的，傳送後會核對地圖
MAX_COMMANDS = 300        # 碧青洞窟第 4、5 步一條路線就有七十幾個指令（原本 60 不夠）
MAX_STEP_ROUNDS = 6          # 同一步最多換幾條分支接著做
OVERRIDE_DIR = Path(__file__).resolve().parent.parent / "data" / "quest_overrides"
CHOICES_FILE = Path(__file__).resolve().parent.parent / "data" / "quest_choices.json"
MAX_WALK_STEPS = 2000
MAX_BATTLES_PER_WALK = 40
MAX_WARP_ATTEMPTS = 6
MAX_WALK_SEGMENTS = 3000
MAX_HUNT_BATTLES = 40
HUNT_FREE_SLOTS = 4          # 打怪取任務道具／抓寵時，每場開打前至少留幾格空位
MERCY_SKILL = "手下留情"
MAX_CAPTURE_BATTLES = 12     # 抓一隻寵物最多打幾場
CAPTURE_ROUNDS = 30          # 一場裡最多下幾回合捕捉，超過就把怪打掉換下一場
MAX_RESYNCS = 300
MAX_SILENT_MOVES = 3
MAX_STACK = 99
EAT_SECONDS = 0.5
ITEM_SETTLE_SECONDS = 1.5
MAX_LEARNED_BLOCKS = 200
SEGMENT_SECONDS = 0.6
POSITION_GRACE_SECONDS = 1.5
WARP_WAIT_SECONDS = 8.0
MINIMUM_HP_PERCENT = 40
NEWLINE_MARK = chr(92) + "n"      # 視窗內文裡的換行記號（反斜線加 n 兩個字元）
PLAN_PATH = Path(__file__).resolve().parent.parent / "captures" / "quest-plan.local.json"
BUTTON_OK, BUTTON_CANCEL, BUTTON_YES, BUTTON_NO, BUTTON_NEXT = 1, 2, 4, 8, 32


@dataclass(frozen=True)
class Progress:
    done: frozenset          # 已完成的任務編號
    steps: dict              # 進行中的任務 → 已完成到第幾步

    def next_step(self, task: int):
        """下一個要做的步驟編號；任務已完成回傳 None。"""
        if task in self.done:
            return None
        return self.steps.get(task, 0) + 1


def parse_progress(mask: bytes, steps: bytes) -> Progress:
    try:
        words = [int(part) for part in mask.decode("ascii").split("|") if part != ""]
        # 伺服器用有號 32 位元整數送（任務 31 完成後第 0 個整數變成 -1879048132），先轉回無號再看位元。
        if len(words) != 32 or any(not -0x80000000 <= word <= 0xFFFFFFFF for word in words):
            raise ValueError
        words = [word & 0xFFFFFFFF for word in words]
        done = frozenset(index * 32 + bit for index, word in enumerate(words) for bit in range(32) if word >> bit & 1)
        current = {}
        for item in steps.decode("ascii").split("|"):
            if item == "":
                continue
            task, _, step = item.partition("-")
            task, step = int(task), int(step)
            if not 0 <= task <= 1024 or not 0 <= step <= 100:
                raise ValueError
            current[task] = step
        return Progress(done, current)
    except (ValueError, UnicodeError):
        raise ProtocolError("任務進度格式不符，已停止") from None


def choose_button(kind: int, buttons: int) -> int:
    """一般訊息視窗要按哪個鈕：有「是」按是，其次「下一頁」，再來「確定」。其他情況停下交給人。"""
    if kind != 0:
        raise ProtocolError(f"沒看過的視窗類型 {kind}，已停止")
    for button in (BUTTON_YES, BUTTON_NEXT, BUTTON_OK):
        if buttons & button:
            return button
    # 只有「否」或「取消」一顆鈕的視窗（成人儀式領完獎勵後的最後一個視窗就是 8）：按下去只是關閉。
    if buttons in (BUTTON_NO, BUTTON_CANCEL):
        return buttons
    raise ProtocolError(f"視窗按鈕 {buttons} 沒有可確定的選項，已停止")


def parse_script(text: str) -> list:
    """把腳本拆成 [(指令, [參數…])]，保留空行讓行號對得上（`I` 的跳轉目標是行號）。"""
    commands = []
    for line in text.replace("\r\n", "\n").split("\n"):
        parts = line.split()
        commands.append((parts[0], parts[1:]) if parts else ("", []))
    return commands


def plan_route(commands: list, *, has_item=lambda name: True, has_pet=lambda name, level=0: True, night=None,
               rebirth=None) -> list:
    """把腳本展開成動作清單：("teleport", 代號)、("map", 地圖)、("walk", x, y)、("talk", 方向)、("note", 文字)。"""
    actions = []
    line = 0
    for _ in range(MAX_COMMANDS):
        if line >= len(commands):
            raise ProtocolError("腳本沒有 END，已停止")
        name, arguments = commands[line]
        line += 1
        try:
            if name == "":
                continue
            if name in ("END", "结束"):
                return actions
            if name in ("B", "回城地图"):
                actions.append(("teleport", int(arguments[0])))
            elif name in ("M", "M1", "地图", "地图1"):
                actions.append(("map", int(arguments[0])))
            elif name in ("G", "坐标"):
                actions.append(("walk", int(arguments[0]), int(arguments[1])))
            elif name == "npc对话":
                actions.append(("talk", int(arguments[0])))
            elif name in ("T", "到达"):
                actions.append(("arrive", int(arguments[0]), int(arguments[1]), int(arguments[2])))
            elif name in ("S", "提示"):
                actions.append(("note", " ".join(arguments)))
            elif name in ("I", "道具"):
                if not has_item(arguments[0]):
                    line = int(arguments[1]) - 1
                    if not 0 <= line < len(commands):
                        raise ValueError
            elif name == "i":
                # `i 道具 行號`（小寫）：身上「有」這樣道具就跳到那一行（玄黃洞窟：帶著風的手鐲不能回記錄點，跳去提示）。
                if has_item(arguments[0]):
                    line = int(arguments[1]) - 1
                    if not 0 <= line < len(commands):
                        raise ValueError
            elif name == "F":
                # `F 名稱 x y`：和站在 (x, y) 的那隻（守衛）開打——走到牠旁邊、朝牠說話。
                actions.append(("duel", int(arguments[-2]), int(arguments[-1]), " ".join(arguments[:-2])))
            elif name == "P":
                # `P 寵物名 最低等級 ? 行號`：身上沒有這隻寵物（或等級不夠）就跳到那一行。
                # 卡坦的願望是 `P 加格 0 0 8`（沒有就去抓）；猜謎三兄弟是 `P 金布伊 77 0 28`（沒練到 77 級就只提示）。
                # 2026-11-18 實測：忽略等級時引擎帶著 1 級的金布伊去交，白花一張入場券。第三個參數還沒看懂。
                if not has_pet(arguments[0], int(arguments[1]) if len(arguments) >= 4 else 0):
                    line = int(arguments[-1]) - 1
                    if len(arguments) < 2 or not 0 <= line < len(commands):
                        raise ValueError
            elif name == "转生":
                # `转生 轉生數 行號`：角色的轉生數不是這個數就跳到那一行（轉生任務的腳本每一轉一段，
                # 抓的守護獸不同）。2026-11-20 讀任務 100 的腳本得出；不知道轉生數就停，不亂走。
                if rebirth is None:
                    raise ProtocolError("讀不到角色的轉生數，已停止")
                if rebirth != int(arguments[0]):
                    line = int(arguments[1]) - 1
                    if not 0 <= line < len(commands):
                        raise ValueError
            elif name in ("DAY", "NIGHT"):
                # `DAY 行號`：現在不是白天就跳到那一行；`NIGHT 行號`：現在不是夜晚就跳（夢幻洞窟白天和夜晚入口不同）。
                if night is None:
                    raise ProtocolError("還不知道遊戲內現在是白天還是夜晚，已停止")
                if night == (name == "DAY"):
                    line = int(arguments[0]) - 1
                    if not 0 <= line < len(commands):
                        raise ValueError
            else:
                raise ProtocolError(f"腳本指令「{name}」還沒看懂，已停止")
        except (ValueError, IndexError):
            raise ProtocolError(f"腳本第 {line} 行格式不符，已停止") from None
    raise ProtocolError("腳本指令數超過上限，已停止")


def rebirth_of(world):
    """角色的轉生數（狀態訊息 P1 第 24 欄）；讀不到回 None。"""
    try:
        return int((world.raw.get("P1") or [])[24])
    except (IndexError, ValueError, AttributeError):
        return None


def load_choices(path: Path = CHOICES_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["選擇"]


PROFILE_FILE = Path(__file__).resolve().parent.parent / "data" / "module_profiles.json"


def load_quest_profile(path: Path = None) -> dict:
    """任務活動的設定（data/module_profiles.json 的「任務」）；讀不到回空的。"""
    try:
        profile = json.loads((path or PROFILE_FILE).read_text(encoding="utf-8"))["任務"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return profile if isinstance(profile, dict) else {}


def load_rallies(path: Path = None) -> tuple:
    """要全隊一起打首領的步驟（「任務-步驟」）：各任務的「集合步驟」。"""
    steps = []
    for task, limits in load_quest_profile(path).get("各任務", {}).items():
        for step in limits.get("集合步驟", ()) if isinstance(limits, dict) else ():
            steps.append(f"{task}-{step}")
    return tuple(steps)


def load_no_return(path: Path = None) -> tuple:
    """帶在身上就不能回記錄點的任務道具名稱。"""
    names = load_quest_profile(path).get("不可回記錄點道具", ())
    return tuple(name for name in names if isinstance(name, str) and name)


def load_mall_items(path: Path = CHOICES_FILE) -> tuple:
    """要到商城石幣分頁買的任務道具名稱。"""
    try:
        return tuple(json.loads(path.read_text(encoding="utf-8"))["商城道具"]["名稱"])
    except (OSError, ValueError, KeyError, TypeError):
        return ()


def apply_choice(actions: list, choice) -> list:
    """腳本結尾只寫「請選擇」而沒有對話時，照設定檔補上「走到哪、朝哪個方向說話」。

    只在動作清單的最後一項就是設定檔寫的那句提示時才套用；腳本改版、提示對不上就原樣交回（之後會照舊停下）。
    領獎只能選一次，所以向誰領一律來自設定檔的實測值，不由程式猜。"""
    if not choice or not actions or actions[-1] != ("note", choice["提示"]):
        return actions
    body, dropped = actions[:-1], int(choice.get("去掉走路", 0))
    if not 0 <= dropped <= len(body) or any(action[0] != "walk" for action in body[len(body) - dropped:]):
        raise ProtocolError("任務選擇設定和腳本對不上（要去掉的不是走路動作），已停止")
    body = body[:len(body) - dropped]
    if "走到" in choice:
        body.append(("walk", int(choice["走到"][0]), int(choice["走到"][1])))
    direction = int(choice["對話方向"])
    if not 0 <= direction <= 7:
        raise ProtocolError("任務選擇設定的對話方向錯誤，已停止")
    return body + [actions[-1], ("talk", direction)]


def teleport_code(value: int) -> int:
    if value < 1000:
        return value
    if value not in VILLAGE_CODES:
        raise ProtocolError(f"還不知道地圖 {value} 的傳送代號，已停止")
    return VILLAGE_CODES[value]


def progress_of(session: Session) -> Progress:
    if session.world.tasks is None:
        raise ProtocolError("沒有收到任務進度，已停止")
    return parse_progress(*session.world.tasks)


def wait_for_map(session: Session, map_id: int, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while session.world.map_id != map_id and time.monotonic() < end:
        session.poll_event(0.3)
    if session.world.map_id != map_id:
        raise ProtocolError(f"預期在地圖 {map_id}，實際在 {session.world.map_id}，已停止")


FIGHTER = None               # 任務模組開了快速戰鬥時換成快戰打法（只用在不逃跑的全隊戰鬥）


def fight_present(session: Session, *, maximum_rounds=40, say=print, allow_escape=True) -> int:
    """戰場上有誰就讓誰打到結束。FIGHTER 有設定、而且這場不准逃跑（全隊戰鬥）時交給它，其餘走一般流程。"""
    if FIGHTER is not None and not allow_escape:
        return FIGHTER(session)
    return fight_plain(session, maximum_rounds=maximum_rounds, say=say, allow_escape=allow_escape)


def fight_plain(session: Session, *, maximum_rounds=40, say=print, allow_escape=True) -> int:
    """不看隊伍編制，戰場上有誰就讓誰普通攻擊，打到戰鬥結束（單人進任務場地、或對話觸發的首領戰用）。

    指令與既有流程相同：人物 `H|目標`、寵物 `W|0|目標`，自己走 C14、其他子號走 C2010；
    每回合先送 ZD、回合演出後送 DD。自己血量低於門檻就改逃跑。回傳打了幾回合。"""
    battle = session.battle
    try:
        ended = {"value": False}

        def wait(seconds, done):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                event = session.poll_event(0.25)
                if event is not None and (event.func == 101 or (event.func == 7 and event.values[0] == 0)):
                    ended["value"] = True
                if ended["value"] or done():
                    return True
            return ended["value"] or done()

        descriptor = session.sock.fileno() if hasattr(session.sock, "fileno") else 0
        for rounds in range(maximum_rounds):
            session._send_command(2025, (f"ZD|{descriptor}|".encode(),), time.monotonic() + session.timeout)
            wait(8.0, lambda: bool(battle.combatants) and not battle.awaiting_round)
            if ended["value"]:
                break
            enemies = [unit for position, unit in sorted(battle.combatants.items()) if position >= 10 and unit.hp > 0]
            me = battle.combatants.get(battle.own_position or 0)
            if not enemies:
                # 名冊還沒到，或敵人已全滅在等結算：等結束通知；等不到就進下一輪再看。
                wait(6.0, lambda: any(position >= 10 and unit.hp > 0 for position, unit in battle.combatants.items()))
                if ended["value"]:
                    break
                continue
            if allow_escape and me is not None and me.hp * 100 < me.max_hp * MINIMUM_HP_PERCENT:
                say("血量過低，改成逃跑")
                session.battle.awaiting_round = False
                return rounds + session.escape_battle(maximum_rounds=20)
            target = enemies[0].position
            if rounds == 0:
                say("對手：" + "、".join(f"{unit.name.decode('gbk', 'replace')} Lv{unit.level}" for unit in enemies))
            for position in range(10):
                unit = battle.combatants.get(position)
                if unit is None or unit.hp <= 0 or battle.ready_mask & (1 << position):
                    continue
                command = f"H|{target:X}".encode() if position < 5 else f"W|0|{target:X}".encode()
                owner = position if position < 5 else position - 5
                if owner == 0:
                    session._send_command(14, (command,), time.monotonic() + session.timeout)
                else:
                    session._send_command(2010, (owner, command), time.monotonic() + session.timeout)
                wait(1.5, lambda: bool(battle.ready_mask & (1 << position)))
                if ended["value"]:
                    break
            if ended["value"]:
                break
            battle.awaiting_round = True
            current = battle.round
            wait(15.0, lambda: not battle.awaiting_round and battle.round != current)
            if ended["value"]:
                break
            session._send_command(2025, (f"DD|{descriptor}|".encode(),), time.monotonic() + session.timeout)
        else:
            raise ProtocolError("戰鬥回合數超過上限，已停止")
        wait(3.0, lambda: ended["value"])
        if not ended["value"]:
            raise ProtocolError("戰鬥沒有收到結束通知，已停止")
        battle.active = False
        session._acknowledge_battle_end(time.monotonic() + session.timeout)
        foes = sum(1 for position, unit in battle.combatants.items() if position >= 10 and unit.hp > 0)
        allies = sum(1 for position, unit in battle.combatants.items() if position < 10 and unit.hp > 0)
        if foes:
            say(f"戰鬥結束時對面還有 {foes} 隻站著、我方 {allies} 個站著（沒有打贏）")
        return rounds + 1
    except BaseException:
        session.close()
        raise


def solo_battle(session: Session) -> bool:
    """這場戰鬥是不是只有自己（看戰場上第 2～5 個人物位置有沒有人；登入時的隊伍名單可能是舊的，不能用）。"""
    end = time.monotonic() + 3.0
    while not session.battle.combatants and time.monotonic() < end:
        session.poll_event(0.3)
    return not any(position in session.battle.combatants for position in range(1, 5))


def fight(session: Session, plan_path: Path = PLAN_PATH) -> None:
    """途中遇敵：五人隊伍齊全就用既有的戰鬥流程打完；單人（例如離隊進任務場地後）就逃跑脫離。"""
    from .supervisor import write_plan
    world = session.world
    if solo_battle(session) or not world.roles.ready():
        # 單人，或隊員跟著進了戰鬥但這條連線沒有完整的隊伍資料（直接用隊長登入、沒重新組隊時）：有誰打誰。
        fight_present(session)
        return
    if world.hp is None or world.max_hp is None or world.hp * 100 < world.max_hp * MINIMUM_HP_PERCENT:
        raise ProtocolError("途中遇敵但血量過低，已停止")
    write_plan(plan_path, world.map_id, positions=None, maximum_enemy_level=250)
    os.environ["oo8y26_TRAINING_PLAN"] = str(plan_path)
    session.test_novice_battle(maximum_rounds=30, maximum_seconds=290)
    session.request_roles()


BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
CHUNK_WIDTH, CHUNK_HEIGHT, MAX_CHUNKS = 30, 20, 400
SERVER_MAP_MARGIN = 45
_server_grids = {}


def decode_base62(text: bytes) -> int:
    value = 0
    for code in text:
        value = value * 62 + BASE62.index(chr(code))
    return value


def parse_map_chunk(values: tuple):
    """S39 `(地圖, x1, y1, x2, y2, "名稱|地面|物件|事件")`：三層各是逗號分隔的 62 進位數字，空字串＝0。
    2026-11-18 與本機地圖檔逐格比對相符。回傳 (x1, y1, 寬, 高, 地面, 物件, 事件)。"""
    try:
        x1, y1, x2, y2 = values[1:5]
        _, ground, objects, events = values[5].split(b"|")
        layers = [[decode_base62(item) if item else 0 for item in layer.split(b",")] for layer in (ground, objects, events)]
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0 or any(len(layer) != width * height for layer in layers):
            raise ValueError
        return (x1, y1, width, height, *layers)
    except (ValueError, IndexError):
        raise ProtocolError("伺服器地圖資料格式不符，已停止") from None


def fetch_server_grid(session: Session, *, say=print, base=None, area=None):
    """向伺服器分塊要地圖資料（C38，每塊最多 30×20）組成可找路的格子。

    客戶端的 map/*.dat 其實是「看過的區域」的快取：沒去過的地方是空的（實測北島 200 的烏魯力村口），
    也可能是舊版（烏力洞窟第四層）。base 給本機格子、area 給 (x1, y1, x2, y2) 時，只要那一塊並蓋在本機資料上；
    不給就要整張圖（太大的圖不要整張）。"""
    from .navigation import MapGrid
    world = session.world
    width, height = world.width, world.height
    if area is None:
        area = (0, 0, width, height)
    area = (max(0, area[0] // CHUNK_WIDTH * CHUNK_WIDTH), max(0, area[1] // CHUNK_HEIGHT * CHUNK_HEIGHT),
            min(width, area[2]), min(height, area[3]))
    key = (world.map_id, width, height, area)
    if key in _server_grids:
        return _server_grids[key]
    regions = [(x, y, min(x + CHUNK_WIDTH, width), min(y + CHUNK_HEIGHT, height))
               for y in range(area[1], area[3], CHUNK_HEIGHT) for x in range(area[0], area[2], CHUNK_WIDTH)]
    if not regions or len(regions) > MAX_CHUNKS:
        raise ProtocolError(f"地圖 {world.map_id} 要的範圍太大（{len(regions)} 塊），已停止")
    say(f"向伺服器要地圖 {world.map_id} 的資料（{len(regions)} 塊）")
    if base is not None and (base.width, base.height) == (width, height):
        ground, objects, flags = (list(layer) for layer in base.layers)
    else:
        ground, objects, flags = [0] * (width * height), [0] * (width * height), [0] * (width * height)
    original, chunks = session._read_event, {}

    def read_event(deadline):
        event = original(deadline)
        if event.func == 39 and event.values[0] == world.map_id:
            chunks[tuple(event.values[1:5])] = event.values
        return event

    session._read_event = read_event
    try:
        for region in regions:
            if session.battle.active or world.map_id != key[0]:
                raise ProtocolError("要地圖資料途中進入戰鬥或換圖，已停止")
            try:
                session._send_command(38, (world.map_id, *region), time.monotonic() + session.timeout)
            except BaseException:
                session.close()
                raise
            end = time.monotonic() + 5.0
            while region not in chunks and time.monotonic() < end:
                session.poll_event(0.2)
            if region not in chunks:
                raise ProtocolError(f"沒有收到地圖 {world.map_id} 範圍 {region} 的資料，已停止")
            x1, y1, chunk_width, chunk_height, tiles, things, events = parse_map_chunk(chunks[region])
            for row in range(chunk_height):
                for column in range(chunk_width):
                    index, source = (y1 + row) * width + x1 + column, row * chunk_width + column
                    ground[index], objects[index] = tiles[source], things[source]
                    flags[index] = 0xC000 | (events[source] & 0x3FFF)
    finally:
        session._read_event = original
    _server_grids[key] = MapGrid(width, height, (tuple(ground), tuple(objects), tuple(flags)))
    return _server_grids[key]


VOID_GROUND = 63      # 洞窟地圖裡沒有地板的黑色區域（強盜洞窟一樓實測）


# 只有這麼大以內的地圖（室內、洞窟）才把地面記成牆。碧青洞窟每層 120×120，牆全畫在地面層
# （4037～4071 一帶），不學就得一格一格撞（2026-11-20 撞了 200 格後停下）；野外世界地圖都遠大於這個數。
WALL_LEARNING_AREA = 160 * 160
STALE_MAP_BLOCKS = 3          # 照本機地圖檔走撞到幾格就改用伺服器地圖
MAX_GATES = 3                 # 一段路最多處理幾個擋路的 NPC
GATE_DIRECTIONS = {b"a": 0, b"c": 2, b"e": 4, b"g": 6}     # 移動代碼 → 對話方向


MOVES = {ord("a"): (0, -1), ord("c"): (1, 0), ord("e"): (0, 1), ord("g"): (-1, 0)}


GROUND_WALLS_FILE = Path(__file__).resolve().parent.parent / "data" / "ground_walls.json"


def known_walls(map_id, path: Path = GROUND_WALLS_FILE) -> set:
    """設定檔裡這張地圖已知不能走的地面編號。"""
    try:
        ranges = json.loads(path.read_text(encoding="utf-8"))["地圖"].get(str(map_id), [])
        return {ground for low, high in ranges for ground in range(int(low), int(high) + 1)}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


LEARNED_WALLS_FILE = Path(__file__).resolve().parent.parent / "captures" / "learned-walls.local.json"


def remembered_walls(map_id, path: Path = None) -> set:
    """先前走這張圖時撞出來的牆壁地面編號（存在本機檔，換隊長、重跑都還在）。"""
    try:
        return set(json.loads((path or LEARNED_WALLS_FILE).read_text(encoding="utf-8")).get(str(map_id), []))
    except (OSError, ValueError, AttributeError):
        return set()


def remember_wall(map_id, ground, path: Path = None) -> None:
    """把新學到的牆壁地面記進本機檔；寫不進去就算了（只是下次要重撞）。"""
    path = path or LEARNED_WALLS_FILE
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[str(map_id)] = sorted(set(data.get(str(map_id), [])) | {int(ground)})
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


class LearnedGrid:
    """包住唯讀地圖，再加上走路時實際撞到的格子。

    地圖檔只記物件的錨點格，大型物件（礁石、珊瑚）實際佔好幾格，佔地資料在還沒解開的圖檔裡；
    所以先照地圖走，撞到走不過去的格子就記下來重新找路（2026-11-18 海底通路一樓實測會撞到）。"""

    def __init__(self, grid, *, allow_loose=False):
        self.grid, self.blocked = grid, set()
        self.allow_loose = allow_loose   # 只有伺服器送來的地圖才開放寬鬆模式（本機檔可能是舊版，先換成伺服器資料再說）
        self.goal = None
        self.relaxed = False
        self.walls = set()            # 撞過的「牆壁地面」編號：室內的牆畫在地面層、物件層是空的（漁村醫院實測）
        self.known = set()            # 設定檔列的已知不能走的地面（data/ground_walls.json），不會被清掉
        self.bumps = {}                # 野外大地圖：每種地面被撞到幾格
        self.loose = False            # 第三級：地板上的物件一律先當成可走（強盜洞窟的岩石 110xx 實測走得過去）
        self.stood = set()            # 伺服器確認站過的格子：地圖檔說有物件也照樣算可走（例如被 NPC 傳到的落點）
        self.width, self.height = grid.width, grid.height

    def walkable(self, point, *, endpoints=()):
        if point in self.stood:
            return True
        if point in self.blocked:
            return False
        if self.walls and point != self.goal and 0 <= point[0] < self.width and 0 <= point[1] < self.height:
            index = point[1] * self.width + point[0]
            # 牆壁地面只在那一格沒有物件時才算牆：橋是蓋在水面上的物件（世界地圖 500 的橋，物件 4 底下是水面 193～196，
            # 2026-11-19 用真實客戶端走過確認），有物件的格子交給後面的物件規則判斷。
            if self.grid.layers[0][index] in self.walls and not self.grid.layers[1][index]:
                return False
        if point == self.goal and 0 <= point[0] < self.width and 0 <= point[1] < self.height:
            return True       # 腳本指定的目的地（常是換圖格）在地圖檔上可能標著物件，一律當作走得進去
        if self.grid.walkable(point, endpoints=endpoints):
            return True
        if self.relaxed:
            # 放寬模式：物件層編號很小的（3～99，如 0x4、0x17、0x1b、0x2c）不是實體圖檔，多半只是地面裝飾；
            # 0x2 是隱形牆，仍然不走。猜錯會在走路時撞到並記成障礙，不會硬闖。
            x, y = point
            if 0 <= x < self.width and 0 <= y < self.height:
                ground, objects, flags = (layer[y * self.width + x] for layer in self.grid.layers)
                if ground and 3 <= objects < 100 and flags & 0xC000 == 0xC000 and flags & 0x3FFF == 0:
                    return True
                # 寬鬆模式（前兩級都找不到路才用）：有地板（不是虛空地面 63）的格子上，物件不管編號都先當成可走。
                # 2026-11-18 強盜洞窟一樓實測：密室和主洞之間只隔一排岩石（11001～11006），伺服器允許直接走過去。
                if (self.loose and ground not in (0, VOID_GROUND) and objects != 2
                        and flags & 0xC000 == 0xC000 and flags & 0x3FFF == 0):
                    return True
        # 門口常是並排的幾格事件格（例：北島 (549,637)、(549,638)），目的地被物件圍住只能從隔壁的事件格進去；
        # 目的地旁兩格內的事件格也算可走——踩上去同樣是換圖。
        goal = self.goal
        if goal is None or max(abs(point[0] - goal[0]), abs(point[1] - goal[1])) > 2:
            return False
        x, y = point
        if not 0 <= x < self.width or not 0 <= y < self.height:
            return False
        ground, objects, flags = (layer[y * self.width + x] for layer in self.grid.layers)
        return bool(ground and objects == 0 and flags & 0xC000 == 0xC000)

    def learn_wall(self, tile) -> bool:
        """撞到的格子上沒有物件時，把它的地面編號記成牆（同一張圖同樣的地面都不走）；站過同樣地面的話就不記。"""
        x, y = tile
        if not 0 <= x < self.width or not 0 <= y < self.height:
            return False
        ground, objects, flags = (layer[y * self.width + x] for layer in self.grid.layers)
        if objects or flags & 0x3FFF or not ground or ground in self.walls:
            return False
        if self.width * self.height > WALL_LEARNING_AREA:
            # 野外大地圖：位置回報晚會造成假撞牆，只撞一次就記會把整片地面記成牆（2026-11-19 世界地圖 500 實測）；
            # 完全不記又得把河、海一格一格撞出來。折衷：同一種地面撞到 3 格才記。
            # 2026-11-19 再實測：就算撞 3 格才記，岸邊（走得過去）也會被記成牆，把橋頭封死。
            # 野外大地圖一律不靠撞來學，改用 data/ground_walls.json 的已知水面。
            return False
        if any(self.grid.layers[0][sy * self.width + sx] == ground for sx, sy in self.stood
               if 0 <= sx < self.width and 0 <= sy < self.height):
            return False
        self.walls.add(ground)
        return True

    def path(self, start, goal, *, maximum_steps=256):
        try:
            return self._path(start, goal, maximum_steps=maximum_steps)
        except ProtocolError:
            if self.walls == self.known:
                raise
            self.walls = set(self.known)  # 記成牆的地面可能記錯了（例如其實是站著 NPC 的地板）：丟掉再找一次；已知的水面留著
            return self._path(start, goal, maximum_steps=maximum_steps)

    def _path(self, start, goal, *, maximum_steps=256):
        self.goal = tuple(goal)
        if not self.relaxed:
            try:
                return type(self.grid).path(self, start, goal, maximum_steps=maximum_steps)
            except ProtocolError:
                self.relaxed = True       # 照嚴格規則找不到路（例：卡魯他那村的落點被裝飾物件圍住），改用放寬模式
        if not self.loose:
            try:
                return type(self.grid).path(self, start, goal, maximum_steps=maximum_steps)
            except ProtocolError:
                if not self.allow_loose:
                    raise
                self.loose = True         # 放寬模式也找不到路：改用寬鬆模式，猜錯會在走路時撞到並記成障礙
        return type(self.grid).path(self, start, goal, maximum_steps=maximum_steps)


def segment_tiles(start: tuple, moves: bytes) -> list:
    """一段移動依序會踩到的格子。"""
    tiles, (x, y) = [], start
    for code in moves:
        dx, dy = MOVES[code]
        x, y = x + dx, y + dy
        tiles.append((x, y))
    return tiles


def blocked_tile(start: tuple, tiles: list, actual: tuple):
    """上一段從 start 依序要踩 tiles，結果人在 actual：回傳沒走進去的那一格；actual 不在這段路上回傳 None。"""
    trail = [start] + list(tiles)
    if actual not in trail[:-1]:
        return None
    return trail[trail.index(actual) + 1]


def walk(session: Session, map_dir: Path, goal: tuple, *, say=print, fight=fight) -> None:
    """走到 goal；被擋住就記下那一格、重新找路；遇敵就打完再走。踩到自動換圖的格子時提早結束。

    伺服器收到每一段移動（C1）時，會在 S41 回報「這一段開始前人實際在哪」。所以上一段有沒有走完，
    要到送出下一段時才知道：回報的位置和這一段的起點不同，就代表上一段中途被擋住。
    最後一段沒有「下一段」可問，改送一筆不含步數的移動來取得回報。"""
    from .navigation import MapGrid
    world, goal = session.world, tuple(goal)
    origin = world.map_id
    from_server = False
    try:
        raw = MapGrid.load(map_dir / f"{origin}.dat")
        if (raw.width, raw.height) != (world.width, world.height):
            raise OSError
    except OSError:
        # 沒有地圖檔（或尺寸不符）：改用伺服器的地圖資料。大地圖不要整張（村外那張要一千多塊），
        # 只要「目前位置到目的地」外擴一圈的那一塊——容器裡沒有地圖檔，短距離移動（換站位）靠這個。
        if world.width * world.height > WHOLE_MAP_CELLS and world.x is not None and world.y is not None:
            left, right = sorted((world.x, goal[0]))
            top, bottom = sorted((world.y, goal[1]))
            raw = fetch_server_grid(session, say=say, area=(left - AREA_MARGIN, top - AREA_MARGIN,
                                                            right + AREA_MARGIN, bottom + AREA_MARGIN))
        else:
            raw = fetch_server_grid(session, say=say)
        from_server = True
    grid, route, battles = LearnedGrid(raw, allow_loose=from_server), None, 0
    grid.known = known_walls(origin)
    grid.walls |= grid.known | remembered_walls(origin)
    believed, previous = (world.x, world.y), None        # 自己推算的位置；上一段的 (起點, 會踩的格子)
    resyncs, stalls, silent = 0, {}, 0
    closest = 10 ** 9
    stale = False                                        # 本機地圖檔撞了好幾次：多半是舊版，改用伺服器的地圖

    def move(x, y, moves):
        world.x = world.y = -1                               # 記號：這一段有沒有收到位置回報
        try:
            session._send_command(1, (x, y, moves), time.monotonic() + session.timeout)
            session.sync()
        except BaseException:
            session.close()
            raise
        end = time.monotonic() + SEGMENT_SECONDS
        while time.monotonic() < end:
            session.poll_event(0.2)

    gates = 0

    def open_gate():
        """路被站在通道上的 NPC 擋住時（事件代號 2 的格子）：走到那一格、朝要去的方向說話，打完牠再繼續。

        2026-11-18 沒落的礦坑四樓實測：「走失的格鲁西斯」站在唯一的通道 (44,27) 上，那一格站得上去但過不去；
        對牠說話（「要往前进吗？」）會進首領戰，打贏後牠消失、路就通了。"""
        nonlocal believed, previous, route
        layers, width = grid.grid.layers, grid.width
        tiles = [(index % width, index // width) for index, flags in enumerate(layers[2]) if flags & 0x3FFF in (2, 5)]
        # 事件代號 5：同一格、NPC 當下不在（2026-11-19 五號到礦坑四樓時 (44,27) 是 5，多半剛被別的角色打掉）；
        # 一樣走上去、朝前進方向說一次話再繼續。
        for tile in sorted(tiles, key=lambda tile: abs(tile[0] - believed[0]) + abs(tile[1] - believed[1])):
            if tile in grid.stood or tile == goal:
                continue
            probe = LearnedGrid(grid.grid, allow_loose=True)
            probe.blocked, probe.stood = set(grid.blocked), set(grid.stood) | {tile}
            try:
                probe.path(believed, tile, maximum_steps=MAX_WALK_STEPS)
                onward = list(probe.path(tile, goal, maximum_steps=MAX_WALK_STEPS))
            except ProtocolError:
                continue
            say(f"路被 {tile} 的事件格擋住，走過去對話")
            walk(session, map_dir, tile, say=say, fight=fight)
            if session.battle.active:
                fight(session)
            if world.map_id != origin or (world.x, world.y) != tile:
                raise ProtocolError(f"沒有站上擋路的事件格 {tile}，已停止")
            for window in session.talk(GATE_DIRECTIONS[onward[0][2][:1]]):
                say(f"［{window['title']}］{window['text']}".replace(NEWLINE_MARK, " "))
            if session.battle.active:
                say(f"擋路的 NPC 開戰，打了 {fight_present(session, say=say)} 回合")
            grid.stood.add(tile)
            believed, previous, route = (world.x, world.y), None, None
            return True
        return False

    for _ in range(MAX_WALK_SEGMENTS):
        if session.battle.active:
            battles += 1
            if battles > MAX_BATTLES_PER_WALK:
                raise ProtocolError("同一段路遇敵次數超過上限，已停止")
            say(f"途中遇敵（第 {battles} 場），先打完")
            fight(session)
            believed, previous, route = (world.x, world.y), None, None
            continue
        if world.map_id != origin:
            return
        if not route:
            grid.stood.add(believed)
            try:
                if stale and not from_server:
                    raise ProtocolError("本機地圖檔和實際不符")
                route = list(grid.path(believed, goal, maximum_steps=MAX_WALK_STEPS)) if believed != goal else []
            except ProtocolError:
                if from_server:
                    if gates < MAX_GATES and open_gate():
                        gates += 1
                        continue
                    raise
                # 本機地圖檔可能是舊版（實測：烏力洞窟第四層伺服器多了一條通道）：改用伺服器的地圖資料再找一次。
                say(f"照本機地圖檔找不到路，改向伺服器要地圖 {origin} 的資料")
                from_server = True
                learned = grid.blocked
                margin = SERVER_MAP_MARGIN        # 只要起點到終點附近那一塊，蓋在本機資料上
                area = (min(believed[0], goal[0]) - margin, min(believed[1], goal[1]) - margin,
                        max(believed[0], goal[0]) + margin, max(believed[1], goal[1]) + margin)
                grid = LearnedGrid(fetch_server_grid(session, say=say, base=raw, area=area), allow_loose=True)
                grid.known = known_walls(origin)
                grid.walls |= grid.known | remembered_walls(origin)
                grid.blocked |= learned
                for tile in learned:
                    grid.learn_wall(tile)
                grid.stood.add(believed)
                try:
                    route = list(grid.path(believed, goal, maximum_steps=MAX_WALK_STEPS))
                except ProtocolError:
                    if gates < MAX_GATES and open_gate():
                        gates += 1
                        continue
                    raise
        x, y, moves = route.pop(0) if route else (goal[0], goal[1], b"")
        move(x, y, moves)
        if session.battle.active or world.map_id != origin:
            continue
        if (world.x, world.y) == (-1, -1):
            # 沒有位置回報：伺服器沒受理這次移動（實測：人其實卡在戰鬥裡時會這樣）。不能當作走到了。
            world.x, world.y = believed
            silent += 1
            if silent >= MAX_SILENT_MOVES:
                raise ProtocolError(f"連續 {silent} 次移動都沒有收到位置回報，無法確認位置，已停止")
            route = None
            continue
        silent = 0
        actual = world.x, world.y
        grid.stood.add(actual)
        if actual == (x, y):
            if not moves:
                return                                       # 已確認站在目的地
            believed, previous = segment_tiles((x, y), moves)[-1], ((x, y), segment_tiles((x, y), moves))
            continue
        # 回報的位置和推算的不同：以伺服器為準重新找路。戰鬥前後、或上一段還沒走完時常差一兩格，不代表有障礙；
        # 同一格連續兩次都沒走進去才記成障礙。
        distance = abs(actual[0] - goal[0]) + abs(actual[1] - goal[1])
        if distance < closest:
            closest, resyncs = distance, 0      # 有在往目的地靠近就重新計數：野外長路會一路撞一路修正（世界地圖 500 實測）
        resyncs += 1
        if resyncs > MAX_RESYNCS:
            raise ProtocolError(f"位置一直對不上（最後在 {actual}），已停止")
        tile = blocked_tile(*previous, actual) if previous else None
        if tile is not None:
            stalls[(actual, tile)] = stalls.get((actual, tile), 0) + 1
            if stalls[(actual, tile)] >= 2:
                if tile == goal:
                    raise ProtocolError(f"目的地 {goal} 走不進去，停在 {actual}，已停止")
                grid.blocked.add(tile)
                if len(grid.blocked) > MAX_LEARNED_BLOCKS:
                    raise ProtocolError("撞到的障礙格太多，已停止")
                say(f"{tile} 走不過去，從 {actual} 繞路")
                if from_server and grid.learn_wall(tile):
                    say(f"地面 {grid.grid.layers[0][tile[1] * grid.width + tile[0]]} 是牆，同樣的地面都不走")
                    remember_wall(origin, grid.grid.layers[0][tile[1] * grid.width + tile[0]])
                if not from_server and len(grid.blocked) >= STALE_MAP_BLOCKS:
                    stale = True
        believed, previous, route = actual, None, None
    raise ProtocolError("走路段數超過上限，已停止")


def enter_warp(session: Session, map_dir: Path, goal: tuple, *, say=print, fight=fight):
    """站在換圖格上送進入指令（C3），等伺服器換圖。迷宮裡常在這時遇敵：打完、走回這格再試。"""
    origin = session.world.map_id
    for _ in range(MAX_WARP_ATTEMPTS):
        end = time.monotonic() + 1.0          # 剛走完最後一步時，遇敵通知可能晚一點才到
        while time.monotonic() < end and not session.battle.active:
            session.poll_event(0.2)
        if not session.battle.active and session.world.map_id == origin:
            try:
                session._send_command(3, (3, 0, goal[0], goal[1], -1), time.monotonic() + session.timeout)
            except BaseException:
                session.close()
                raise
            end = time.monotonic() + WARP_WAIT_SECONDS
            while time.monotonic() < end and not session.battle.active and session.world.map_id == origin:
                session.poll_event(0.3)
        if session.world.map_id != origin:
            session.sync()
            return session.world.map_id, session.world.x, session.world.y
        if not session.battle.active:
            raise ProtocolError(f"進入換圖點後地圖沒有改變（實際位置 {session.world.x},{session.world.y}），已停止")
        say("換圖點前遇敵，先打完")
        fight(session)
        walk(session, map_dir, goal, say=say, fight=fight)
    raise ProtocolError("換圖點前遇敵次數超過上限，已停止")


def leave_party(session: Session, *, say=print) -> None:
    """隊長離開隊伍（C52，原版石器的 PR，第三個參數 0＝離隊）。

    試驗中（2026-11-18）：成人儀式的入口 NPC 對五人隊伍只說「无法参加宴会」而不傳送，
    推測這類把人傳進任務場地的 NPC 只收單人，所以對話前先離隊。隊員由練功總控下一圈重新召喚。"""
    world = session.world
    try:
        session._send_command(52, (world.x, world.y, 0), time.monotonic() + session.timeout)
    except BaseException:
        session.close()
        raise
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        session.poll_event(0.3)
    roles = session.request_roles()
    say("離隊後隊伍狀態：" + "、".join(f"{role.slot}:{role.status}" for role in roles))


def hunt(session: Session, item: str, *, say=print) -> None:
    """站在原地遇敵打怪，直到自己的背包出現指定道具（有場數上限）。

    2026-11-18 實測：五人一起打 40 場，隊長一個「良质的木材」都沒拿到——掉落物是全隊分，
    而隊員的背包早被肉塞滿。所以改成隊長先離隊、單人打，掉的東西才會進隊長背包。"""
    world = session.world
    if not world.has_item(item):
        if session.battle.active:
            fight_present(session, say=lambda message: None)
        session.request_roles()
        if any(role.status == 2 for role in world.roles.roles.values()):
            leave_party(session, say=say)
            if any(role.status == 2 for role in world.roles.roles.values()):
                raise ProtocolError("離隊沒有成功，不單人打怪，已停止")
    say(f"單人原地遇敵打怪，直到拿到「{item}」")
    seen = set()
    for battles in range(MAX_HUNT_BATTLES):
        if world.has_item(item):
            say(f"拿到「{item}」（打了 {battles} 場）；遇過的怪：{'、'.join(sorted(seen)) or '無'}")
            return
        if world.free_bag_slots() < HUNT_FREE_SLOTS:
            # 一場會同時掉好幾樣（肉先佔格）：只剩一兩格時任務道具會掉不進來（使用者 2026-11-19 提醒；
            # 四寶玉之謎二號、五號各打 40 場沒拿到寶珠）。每場開打前都留足空位。
            make_room(session, HUNT_FREE_SLOTS + 1, say=lambda message: None)
            if world.free_bag_slots() < 1:
                raise ProtocolError("背包沒有空位，任務道具掉不進來，已停止")
        if not session.battle.active:
            try:
                session._send_command(202, (17,), time.monotonic() + session.timeout)      # 原地遇敵
            except BaseException:
                session.close()
                raise
            end = time.monotonic() + 10.0
            while not session.battle.active and time.monotonic() < end:
                session.poll_event(0.2)
            if not session.battle.active:
                raise ProtocolError("原地遇敵沒有進入戰鬥，已停止")
            try:
                session._send_command(202, (18,), time.monotonic() + session.timeout)      # 取消後續遇敵
            except BaseException:
                session.close()
                raise
        end = time.monotonic() + 4.0
        while not session.battle.combatants and time.monotonic() < end:
            session.poll_event(0.2)
        seen.update(f"{unit.name.decode('gbk', 'replace')} Lv{unit.level // 10 * 10}～" for position, unit in session.battle.combatants.items()
                    if position >= 10)
        fight_present(session, say=lambda message: None)
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            session.poll_event(0.3)
    if not world.has_item(item):
        raise ProtocolError(f"打了 {MAX_HUNT_BATTLES} 場還沒拿到「{item}」（遇過的怪：{'、'.join(sorted(seen))}），已停止")


def capture_battle(session: Session, name: str, *, maximum_rounds=60) -> bool:
    """打一場：場上有叫 name 的怪就對牠下捕捉（`T|目標`，原版石器的捕捉指令），其餘的怪照常打掉。

    寵物只打別的怪，不碰要抓的那一隻；只剩牠時寵物送 `W|FF|FF`（逃跑流程實測過伺服器會收的「不動作」）。
    只盯第一眼看到的那一隻；牠不在場上了（抓到或跑掉）就把剩下的打完。回傳這一場有沒有下過捕捉指令。"""
    battle = session.battle
    try:
        ended = {"value": False}

        def wait(seconds, done):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                event = session.poll_event(0.25)
                if event is not None and (event.func == 101 or (event.func == 7 and event.values[0] == 0)):
                    ended["value"] = True
                if ended["value"] or done():
                    return True
            return ended["value"] or done()

        descriptor = session.sock.fileno() if hasattr(session.sock, "fileno") else 0
        wanted, tried = None, False
        # 戰寵會「手下留情」（商城石幣分頁的技能罐頭）就先用它削血；滿血的怪實測幾乎抓不到（約 350 次全失敗）。
        skills = session.world.pet_skills.get(session.world.battle_pet_index, [])
        mercy = skills.index(MERCY_SKILL) if MERCY_SKILL in skills else None
        for rounds in range(maximum_rounds):
            session._send_command(2025, (f"ZD|{descriptor}|".encode(),), time.monotonic() + session.timeout)
            wait(8.0, lambda: bool(battle.combatants) and not battle.awaiting_round)
            if ended["value"]:
                break
            enemies = [unit for position, unit in sorted(battle.combatants.items()) if position >= 10 and unit.hp > 0]
            if not enemies:
                wait(6.0, lambda: any(position >= 10 and unit.hp > 0 for position, unit in battle.combatants.items()))
                if ended["value"]:
                    break
                continue
            me = battle.combatants.get(battle.own_position or 0)
            if me is not None and me.hp * 100 < me.max_hp * MINIMUM_HP_PERCENT:
                session.battle.awaiting_round = False
                session.escape_battle(maximum_rounds=20)
                return tried
            if rounds == 0:
                named = [unit.position for unit in enemies if unit.name.decode("gbk", "replace") == name
                         and (CAPTURE_LEVEL is None or unit.level == CAPTURE_LEVEL)]
                wanted = named[0] if named else None
            if rounds >= CAPTURE_ROUNDS:
                wanted = None                    # 這一場抓不到：把剩下的打掉，換下一場
            target = next((unit for unit in enemies if unit.position == wanted), None)
            others = [unit for unit in enemies if unit.position != wanted]
            if CAPTURE_TRACE is not None and wanted is not None:
                pet_unit = battle.combatants.get(5)
                CAPTURE_TRACE(f"  第 {rounds + 1} 回合：目標 {'血量 %d/%d' % (target.hp, target.max_hp) if target else '不在場上了'}；"
                              f"其他怪 {len(others)} 隻；自己 {me.hp if me else '?'}/{me.max_hp if me else '?'}；"
                              f"戰寵 {pet_unit.hp if pet_unit else '沒上場'}；手下留情欄位 {mercy}")
            for position in (0, 5):
                unit = battle.combatants.get(position)
                if unit is None or unit.hp <= 0 or battle.ready_mask & (1 << position):
                    continue
                if position == 0:
                    command = f"T|{target.position:X}" if target else f"H|{others[0].position:X}"
                    tried = tried or target is not None
                elif target is not None and mercy is not None and target.hp > 1:
                    command = f"W|{mercy:X}|{target.position:X}"        # 手下留情：把目標打到剩 1 滴血，不會打死
                else:
                    command = f"W|0|{others[0].position:X}" if others else "W|FF|FF"
                session._send_command(14, (command.encode(),), time.monotonic() + session.timeout)
                wait(1.5, lambda: bool(battle.ready_mask & (1 << position)))
                if ended["value"]:
                    break
            if ended["value"]:
                break
            battle.awaiting_round = True
            current = battle.round
            wait(15.0, lambda: not battle.awaiting_round and battle.round != current)
            if ended["value"]:
                break
            session._send_command(2025, (f"DD|{descriptor}|".encode(),), time.monotonic() + session.timeout)
        else:
            raise ProtocolError("戰鬥回合數超過上限，已停止")
        wait(3.0, lambda: ended["value"])
        if not ended["value"]:
            raise ProtocolError("戰鬥沒有收到結束通知，已停止")
        battle.active = False
        session._acknowledge_battle_end(time.monotonic() + session.timeout)
        return tried
    except BaseException:
        session.close()
        raise


def capture_party(session: Session, name: str, *, level=None, maximum_battles=600, heal_below=60, say=print) -> None:
    """全隊一起原地遇敵，直到隊長的寵物欄出現叫 name（指定等級）的寵物。開快戰，不送回合同步。

    沒有目標的場次全隊用快戰打掉；有目標時隊長下捕捉（`T|目標`）、隊長的戰寵用手下留情把牠削到 1 滴血，
    四隻隊員和牠們的寵物只打其他的怪，其他的怪清完就防禦／不動作。2026-11-21：隊長單人在 93 級怪堆裡抓 1 級的
    布林帖斯被打死五次，所以改成全隊。每場前隊長血量低於 heal_below 就用免費烤豬排補。"""
    from . import modules
    world = session.world      # 注意：session.battle 每場戰鬥會換成新的物件，不能先存起來用

    def match(unit):
        return unit.name.decode("gbk", "replace") == name and (level is None or unit.level == level)

    def have():
        return any(pet.name.decode("gbk", "replace") == name and (level is None or pet.level == level)
                   for pet in world.pets.values())

    if have():
        return
    if len(world.pets) >= 5:
        raise ProtocolError("寵物欄滿了，沒有位置放抓到的寵物，已停止")
    fighter = world.battle_pet_index
    if fighter not in world.pets:
        # 重組隊伍後這條連線有時不知道自己的戰寵是哪一隻：找會手下留情的那隻，設成出戰
        fighter = next((index for index in world.pets if MERCY_SKILL in world.pet_skills.get(index, [])), None)
        if fighter is None:
            raise ProtocolError("隊長身上沒有會手下留情的寵物，已停止")
        # 只是這條連線不知道索引，不重送出戰指令：2026-11-21 在這裡重設戰寵之後，送出遇敵就收不到開戰通知
    skills = world.pet_skills.get(fighter, [])
    if MERCY_SKILL not in skills:
        raise ProtocolError(f"隊長的戰寵（寵物欄第 {fighter} 格）還不會手下留情（技能：{[x for x in skills if x]}），已停止")
    mercy = skills.index(MERCY_SKILL)
    spot_map, seen, attempts = world.map_id, set(), 0
    try:
        session._send_command(2025, (f"KS|{session.sock.fileno()}|1|".encode(),), time.monotonic() + 20)
    except BaseException:
        session.close()
        raise
    session._fast_battle_on = True
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        session.poll_event(0.25)

    def pump(seconds, done):
        ended = False
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.05)
            if event is not None and (event.func == 101 or (event.func == 7 and event.values[0] == 0)):
                ended = True
            if ended or done():
                break
        return ended

    started = time.monotonic()
    for battles in range(maximum_battles):
        if have():
            say(f"抓到 {level if level is not None else ''} 級的「{name}」（打了 {battles} 場，其中 {attempts} 場下過捕捉）")
            return
        if battles and battles % 10 == 0:
            say(f"已打 {battles} 場（每場 {(time.monotonic() - started) / battles:.1f} 秒），下過捕捉 {attempts} 場；"
                f"隊長血量 {solo_hp_percent(world)}%；出現過：{'、'.join(sorted(seen))[:120]}")
        if world.map_id != spot_map:
            raise ProtocolError(f"人已經不在抓寵的地圖（現在在 {world.map_id}），已停止")
        if not session.battle.active and heal_below and solo_hp_percent(world) < heal_below:
            heal_with_items(session, say=say)
        if not session.battle.active:
            for attempt in range(2):
                try:
                    session._send_command(202, (17,), time.monotonic() + session.timeout)
                except BaseException:
                    session.close()
                    raise
                end = time.monotonic() + 10.0
                while not session.battle.active and time.monotonic() < end:
                    session.poll_event(0.2)
                if not session.battle.active:
                    # 2026-11-21：帶隊時送出遇敵後這條連線沒收到開戰通知，其實已經在戰鬥裡；要一次名冊伺服器才會補送
                    session.request_roles()
                    end = time.monotonic() + 3.0
                    while not session.battle.active and time.monotonic() < end:
                        session.poll_event(0.2)
                if session.battle.active:
                    break
            if not session.battle.active:
                raise ProtocolError("原地遇敵沒有進入戰鬥，已停止")
            try:
                session._send_command(202, (18,), time.monotonic() + session.timeout)
            except BaseException:
                session.close()
                raise
        end = time.monotonic() + 4.0
        while not session.battle.combatants and time.monotonic() < end:
            session.poll_event(0.2)
        seen.update(f"{unit.name.decode('gbk', 'replace')} Lv{unit.level}"
                    for position, unit in session.battle.combatants.items() if position >= 10)
        if not any(position >= 10 and match(unit) for position, unit in session.battle.combatants.items()):
            modules.fast_fight(session)
            continue
        say(f"第 {battles + 1} 場出現目標，隊長下捕捉、隊員清其他的怪")
        attempts += 1
        wanted, ended = None, False
        try:
            for rounds in range(CAPTURE_ROUNDS + 30):
                ended = pump(8.0, lambda: bool(session.battle.combatants) and not session.battle.awaiting_round)
                if ended:
                    break
                enemies = [unit for position, unit in sorted(session.battle.combatants.items()) if position >= 10 and unit.hp > 0]
                if not enemies:
                    ended = pump(6.0, lambda: any(p >= 10 and u.hp > 0 for p, u in session.battle.combatants.items()))
                    if ended:
                        break
                    continue
                if wanted is None:
                    wanted = next((unit.position for unit in enemies if match(unit)), -1)
                target = next((unit for unit in enemies if unit.position == wanted), None)
                if rounds >= CAPTURE_ROUNDS:
                    target = None                 # 抓太久了：這一隻放棄，全部打掉換下一場
                others = [unit for unit in enemies if target is None or unit.position != target.position]
                for position in range(10):
                    unit = session.battle.combatants.get(position)
                    if unit is None or unit.hp <= 0 or session.battle.ready_mask & (1 << position):
                        continue
                    if position == 0:
                        command = f"T|{target.position:X}" if target else f"H|{others[0].position:X}"
                    elif position == 5 and target is not None and target.hp > 1:
                        command = f"W|{mercy:X}|{target.position:X}"
                    elif position < 5:
                        command = f"H|{others[0].position:X}" if others else "G"
                    else:
                        command = f"W|0|{others[0].position:X}" if others else "W|FF|FF"
                    owner = position if position < 5 else position - 5
                    if owner == 0:
                        session._send_command(14, (command.encode(),), time.monotonic() + session.timeout)
                    else:
                        session._send_command(2010, (owner, command.encode()), time.monotonic() + session.timeout)
                session.battle.awaiting_round = True
                current = session.battle.round
                ended = pump(35.0, lambda: not session.battle.awaiting_round and session.battle.round != current)
                if ended:
                    break
            else:
                raise ProtocolError("抓寵那一場回合數超過上限，已停止")
            pump(3.0, lambda: False)
            session.battle.active = False
        except BaseException:
            session.close()
            raise
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            session.poll_event(0.3)
    if not have():
        raise ProtocolError(f"打了 {maximum_battles} 場（{attempts} 場下過捕捉）還沒抓到「{name}」"
                            f"（出現過：{'、'.join(sorted(seen))[:200]}），已停止")


MERCY_PRICE = 10000          # 商城石幣分頁「手下留情」罐頭的價格上限（2026-11-19 實測 10000 石幣）


def teach_mercy(session: Session, *, say=print) -> None:
    """戰寵還不會「手下留情」就到商城石幣分頁買罐頭教牠（使用者 2026-11-19 指示的抓寵做法）。

    實測流程：C17 (x, y, 道具格, 寵物欄+1) → S66 (41, 0, seqno, -1, …) 選技能欄視窗 →
    C67 (x, y, seqno, -1, 0, "技能欄編號") → 寵物技能欄更新、道具消失。只放進空的技能欄，沒有空欄就停。"""
    world = session.world
    pet = world.battle_pet_index
    skills = world.pet_skills.get(pet)
    if pet not in world.pets or not skills:
        raise ProtocolError("還不知道戰寵的技能欄，不教技能，已停止")
    if MERCY_SKILL in skills:
        return
    empty = [slot for slot, skill in enumerate(skills[:7]) if not skill]
    if not empty:
        raise ProtocolError("戰寵沒有空的技能欄可以學「手下留情」，已停止")
    from .mall import buy_with_stone
    if not world.has_item(MERCY_SKILL):
        if world.free_bag_slots() < 1:
            make_room(session, 1, say=say)
        buy_with_stone(session, [MERCY_SKILL], say=say, maximum_price=MERCY_PRICE)
    slot = next(key for key, value in world.items.items() if key >= 9 and value[0] == MERCY_SKILL)
    try:
        time.sleep(1.0)
        session._send_command(17, (world.x, world.y, slot, pet + 1), time.monotonic() + session.timeout)
        window = None
        end = time.monotonic() + 6.0
        while window is None and time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is not None and event.func == 66 and event.values[0] == 41:
                window = event
        if window is None:
            raise ProtocolError("使用技能罐頭後沒有出現選技能欄的視窗，已停止")
        time.sleep(1.0)
        session._send_command(67, (world.x, world.y, window.values[2], window.values[3], 0, b"%d" % empty[0]),
                              time.monotonic() + session.timeout)
        end = time.monotonic() + 6.0
        while MERCY_SKILL not in world.pet_skills.get(pet, []) and time.monotonic() < end:
            session.poll_event(0.3)
    except BaseException:
        session.close()
        raise
    if MERCY_SKILL not in world.pet_skills.get(pet, []):
        raise ProtocolError("戰寵沒有學會「手下留情」，已停止")
    say(f"戰寵「{world.pets[pet].name.decode('gbk', 'replace')}」學會手下留情（第 {empty[0] + 1} 個技能欄）")


def capture(session: Session, name: str, *, say=print) -> None:
    """站在原地遇敵，直到寵物欄出現叫 name 的寵物（有場數上限）。隊長先離隊單人抓，寵物才會進自己的寵物欄。"""
    world = session.world
    if world.has_pet(name):
        return
    if len(world.pets) >= 5:
        raise ProtocolError("寵物欄滿了，沒有位置放抓到的寵物，已停止")
    if session.battle.active:
        fight_present(session, say=lambda message: None)
    session.request_roles()
    if any(role.status == 2 for role in world.roles.roles.values()):
        leave_party(session, say=say)
        if any(role.status == 2 for role in world.roles.roles.values()):
            raise ProtocolError("離隊沒有成功，不單人抓寵，已停止")
    if world.battle_pet_index not in world.pets:
        # 戰寵沒有出戰（例如上一場被打倒）就抓不了：手下留情要靠牠放。
        session.ensure_pets(slots=(world.roles.controlled_slot,))
    teach_mercy(session, say=say)
    say(f"單人原地遇敵，直到抓到「{name}」")
    seen, attempts = set(), 0
    spot_map = world.map_id
    for battles in range(MAX_CAPTURE_BATTLES):
        if world.has_pet(name):
            say(f"抓到「{name}」（打了 {battles} 場，其中 {attempts} 場下過捕捉）")
            return
        if battles and battles % 50 == 0:
            say(f"已打 {battles} 場，下過捕捉 {attempts} 場，還沒抓到；出現過：{'、'.join(sorted(seen))[:160]}")
        if world.free_bag_slots() < HUNT_FREE_SLOTS:
            # 一場會同時掉好幾樣（肉先佔格）：只剩一兩格時任務道具會掉不進來（使用者 2026-11-19 提醒；
            # 四寶玉之謎二號、五號各打 40 場沒拿到寶珠）。每場開打前都留足空位。
            make_room(session, HUNT_FREE_SLOTS + 1, say=lambda message: None)
            if world.free_bag_slots() < 1:
                raise ProtocolError("背包沒有空位，任務道具掉不進來，已停止")
        if CAPTURE_FIGHTER and not session.battle.active:
            # 戰寵被打倒會被撤下（戰寵索引變 -1），之後人物單獨挨打、也沒有手下留情可用：每場前確認牠還在場上。
            # 2026-11-21 隊長連續被打倒 12 次，逐回合紀錄才看出「戰寵沒上場」。
            own = world.roles.controlled_slot
            actor = world.actor(own)
            index = next((i for i, pet in actor.pets.items()
                          if pet.name.decode("gbk", "replace") == CAPTURE_FIGHTER), None)
            if index is not None and actor.battle_pet_index != index and actor.pets[index].hp > 0:
                session._set_pet_work(own, index, 1)
                session._set_battle_pet(own, index)
                say(f"戰寵「{CAPTURE_FIGHTER}」不在場上，重新派上場")
        if CAPTURE_HEAL_BELOW and not session.battle.active and solo_hp_percent(world) < CAPTURE_HEAL_BELOW:
            # 單人連續打高等怪血會一路掉：2026-11-21 隊長抓布林帖斯時被打死送回村子三次（看起來像「遇敵沒有進入戰鬥」）。
            heal_with_items(session, say=say)
        if world.map_id != spot_map:
            raise ProtocolError(f"人已經不在抓寵的地圖（現在在 {world.map_id}，多半是被打倒送回記錄點），已停止")
        if not session.battle.active:
            for attempt in range(2):
                # 偶爾送出遇敵後沒有開打（練功模組也遇過）：只補送一次，還是沒有才停
                try:
                    session._send_command(202, (17,), time.monotonic() + session.timeout)      # 原地遇敵
                except BaseException:
                    session.close()
                    raise
                end = time.monotonic() + 10.0
                while not session.battle.active and time.monotonic() < end:
                    session.poll_event(0.2)
                if session.battle.active:
                    break
            if not session.battle.active:
                raise ProtocolError("原地遇敵沒有進入戰鬥，已停止")
            try:
                session._send_command(202, (18,), time.monotonic() + session.timeout)      # 取消後續遇敵
            except BaseException:
                session.close()
                raise
        end = time.monotonic() + 4.0
        while not session.battle.combatants and time.monotonic() < end:
            session.poll_event(0.2)
        seen.update(f"{unit.name.decode('gbk', 'replace')} Lv{unit.level}"
                    for position, unit in session.battle.combatants.items() if position >= 10)
        present = any(position >= 10 and unit.name.decode("gbk", "replace") == name
                      and (CAPTURE_LEVEL is None or unit.level == CAPTURE_LEVEL)
                      for position, unit in session.battle.combatants.items())
        if CAPTURE_ESCAPE and not present:
            # 這一場沒有要抓的：直接逃跑，只挨一回合（單人對一整群高等怪硬打會被打死，2026-11-21 隊長死了四次）
            session.escape_battle(maximum_rounds=20)
        elif CAPTURE_FAST and not present:
            # 這一場沒有要抓的：用快戰打法打掉（使用者 2026-11-21：抓一級寵要用快戰）
            from . import modules
            if getattr(session, "_fast_battle_on", False) is not True:
                session._send_command(2025, (f"KS|{session.sock.fileno()}|1|".encode(),), time.monotonic() + 20)
                session._fast_battle_on = True
            modules.fast_fight(session)
        else:
            if present and CAPTURE_LEVEL is not None:
                say(f"出現 {CAPTURE_LEVEL} 級的「{name}」（第 {battles + 1} 場），下捕捉")
            attempts += capture_battle(session, name)
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            session.poll_event(0.3)
    if not world.has_pet(name):
        raise ProtocolError(f"打了 {MAX_CAPTURE_BATTLES} 場（{attempts} 場下過捕捉）還沒抓到「{name}」"
                            f"（遇過的怪：{'、'.join(sorted(seen))}），已停止")


def task_pets(task: int, script_dir: Path = SCRIPT_DIR) -> set:
    """這個任務所有步驟的腳本裡提到的寵物名稱（`P` 的第一個參數）：要交給 NPC 的，不能存倉庫。"""
    names = set()
    for path in script_dir.glob(f"{task}-*.txt"):
        for name, arguments in parse_script(path.read_text(encoding="utf-8")):
            if name == "P" and arguments:
                names.add(arguments[0])
    return names


def task_items(task: int, script_dir: Path = SCRIPT_DIR, *, first_step: int = 1) -> set:
    """這個任務（從第 first_step 步起）的腳本裡提到的道具名稱（`I`／`道具有` 等指令的第一個參數）。"""
    names = set()
    for path in script_dir.glob(f"{task}-*.txt"):
        try:
            if int(path.stem.split("-")[1]) < first_step:
                continue
        except (IndexError, ValueError):
            continue
        for name, arguments in parse_script(path.read_text(encoding="utf-8")):
            if name in ("I", "i", "道具", "道具有") and arguments:
                names.add(arguments[0])
    return names


def resume_index(actions: list, map_id) -> int:
    """人已經在路線中途的某張地圖時，從那張地圖的最後一次出現接著走，不必回城重來。"""
    for index in range(len(actions) - 1, 0, -1):
        if actions[index] == ("map", map_id):
            # 同一張圖連著出現兩次（中間沒有別的地圖）：後一次是圖內被 NPC 傳送之後的那一段
            # （碧青洞窟 21308：先在 (20,11) 跟守衛說話，被傳到圍起來的 (20,7) 再找賢者）。
            # 人剛進這張圖時還在前一段，從前一次出現接起。
            for earlier in range(index - 1, 0, -1):
                if actions[earlier][0] == "map":
                    if actions[earlier][1] == map_id:
                        return earlier
                    break
            return index
    return 0


SOLO_HEAL_BELOW = 60          # 單人解任務時，人物或戰寵血量低於這個百分比就先回薩村村口補血
HEAL_MAP, HEAL_POSITION = 100, (637, 493)     # 薩村外：這張圖戰後全隊補滿（練功總控的補血點，怪 10 級以下）


def solo_hp_percent(world) -> int:
    """自己和戰寵、騎寵的最低血量百分比；資料不完整回 100（不因為不知道就去補血）。"""
    units = [(world.hp, world.max_hp)]
    for index in (world.mount_pet_index, world.battle_pet_index):
        if index in world.pets:
            units.append((world.pets[index].hp, world.pets[index].max_hp))
    return min((hp * 100 // maximum for hp, maximum in units if hp is not None and maximum), default=100)


# 各記錄點走到村外（地圖 100，戰後全隊補滿）的路：記錄點地圖 → [(要走到的換圖格, 換過去的地圖)…]。
# 漁村（2026-11-20 無畫面實走）：村長家 2006 的門 (20,21) → 漁村 2000 (56,48) → 東門 (108,80) → 地圖 100 (114,661)。
GATE_ROUTES = {2006: (((20, 21), 2000), ((108, 80), 100)), 2000: (((108, 80), 100),)}


def go_to_gate(session: Session, map_dir: Path, *, say=print) -> None:
    """走到村外（地圖 100）。記錄點在薩姆吉爾的走原本驗證過的醫院、村口路線；在漁村的照 GATE_ROUTES 走。
    其他記錄點沒有驗證過的路線：停止，不亂走。"""
    world = session.world
    if world.map_id == HEAL_MAP:
        return
    if world.map_id not in (1000, RECORD_MAP) and world.map_id not in GATE_ROUTES:
        session.return_to_record()
        time.sleep(1.0)
    if world.map_id == RECORD_MAP:
        session.leave_sam_hospital()
    if world.map_id == 1000:
        session.leave_sam_village(map_dir / "1000.dat")
    elif world.map_id in GATE_ROUTES:
        for goal, landing in GATE_ROUTES[world.map_id]:
            walk(session, map_dir, goal, say=lambda message: None)
            enter_warp(session, map_dir, goal, say=say)
            if world.map_id != landing:
                raise ProtocolError(f"出村路線走到地圖 {world.map_id}，不是預期的 {landing}，已停止")
            time.sleep(0.8)
    if world.map_id != HEAL_MAP:
        raise ProtocolError(f"記錄點在地圖 {world.map_id}，沒有驗證過的出村路線，已停止")


def heal_solo(session: Session, map_dir: Path, *, say=print) -> None:
    """單人補血：回記錄點 → 走出薩村 → 在村口原地遇敵打一場（那張圖戰後補滿）。打的是 10 級以下的怪，不逃跑。"""
    world = session.world
    before = solo_hp_percent(world)
    if session.battle.active:
        fight_present(session, say=say)
    if world.map_id != HEAL_MAP:
        go_to_gate(session, map_dir, say=say)
    if world.map_id != HEAL_MAP:
        raise ProtocolError("沒有走到薩村村口，不補血，已停止")
    try:
        session._send_command(202, (17,), time.monotonic() + session.timeout)      # 原地遇敵
        end = time.monotonic() + 10.0
        while not session.battle.active and time.monotonic() < end:
            session.poll_event(0.2)
        if not session.battle.active:
            raise ProtocolError("村口原地遇敵沒有進入戰鬥，已停止")
        session._send_command(202, (18,), time.monotonic() + session.timeout)      # 取消後續遇敵
    except BaseException:
        session.close()
        raise
    end = time.monotonic() + 4.0
    while not session.battle.combatants and time.monotonic() < end:
        session.poll_event(0.2)
    if any(unit.level > 20 for position, unit in session.battle.combatants.items() if position >= 10):
        raise ProtocolError("村口遇到的怪等級不對，不打，已停止")
    fight_present(session, say=lambda message: None, allow_escape=False)
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        session.poll_event(0.3)
    say(f"村口補血：{before}% → {solo_hp_percent(world)}%")


HEAL_ITEM = "烤猪排"           # 商城石幣分頁 0 石幣，「耐300回复(1体)」
MAX_HEAL_ITEMS = 16


def heal_with_items(session: Session, *, say=print) -> None:
    """不用回村的補血：到商城石幣分頁拿免費的烤豬排，對自己、騎寵、戰寵各用到血量夠為止。
    帶著不能回記錄點的任務道具（夢幻洞窟的先見之光）時只能這樣補。每用一個都等伺服器回報道具消失。"""
    from .mall import buy_with_stone
    world = session.world
    before = solo_hp_percent(world)

    def percent(target):
        if target == 0:
            return world.hp * 100 // world.max_hp if world.max_hp else 100
        pet = world.pets.get(target - 1)
        return pet.hp * 100 // pet.max_hp if pet and pet.max_hp else 100

    targets = [0] + [index + 1 for index in (world.mount_pet_index, world.battle_pet_index) if index in world.pets]
    used = 0
    for target in targets:
        while percent(target) < 85:
            if used >= MAX_HEAL_ITEMS:
                raise ProtocolError("補血用掉的道具數超過上限，已停止")
            if not world.has_item(HEAL_ITEM):
                if world.free_bag_slots() < 1:
                    make_room(session, 1, say=say)
                buy_with_stone(session, [HEAL_ITEM], say=lambda message: None, maximum_price=0)
            slot = next(key for key, value in world.items.items() if key >= 9 and value[0] == HEAL_ITEM)
            was = world.items[slot]
            time.sleep(1.0)
            _send_item(session, 17, (world.x, world.y, slot, target))
            if not _item_changed(session, slot, was, 4.0):
                raise ProtocolError("使用補血道具伺服器沒有受理，已停止")
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                session.poll_event(0.3)
            used += 1
    if used:
        say(f"用了 {used} 個{HEAL_ITEM}補血：{before}% → {solo_hp_percent(world)}%")


WHOLE_MAP_CELLS = 40000      # 沒有地圖檔時，格子數超過這個就不向伺服器要整張圖，只要路線附近那一塊
AREA_MARGIN = 30             # 只要一塊時，起點與終點往外多要幾格
FREE_ROUTE = False           # 跑不屬於任何任務的路線（抓寵指引）：不檢查任務進度；要搭配 STOP_AFTER_CAPTURE
CAPTURE_LEVEL = None         # 只抓這個等級的（騎寵一律抓 1 級的，使用者 2026-11-21）；None＝不限
CAPTURE_HEAL_BELOW = 0       # 抓寵時自己或寵物血量低於這個百分比就先用免費烤豬排補（0＝不補）
CAPTURE_TRACE = None         # 給一個函式就會逐回合印出抓寵那一場的狀況（除錯用）
CAPTURE_FIGHTER = ""         # 抓寵時每場前確認這隻戰寵在場上（被打倒會被撤下）
CAPTURE_ESCAPE = False       # 沒有要抓的那幾場直接逃跑（單人到高等怪的地方抓 1 級寵用）
CAPTURE_FAST = False         # 沒有要抓的那幾場用快戰打法打掉
CARRY_ITEMS = ()             # 這個任務要帶在身上的道具名稱（不收進倉庫；缺了從倉庫領，領不齊就停）
STOP_AFTER_CAPTURE = False   # 抓到腳本要的寵物後先停下來，不接著做下一段（轉生前要先讓人確認）
STORE_SPARE_PETS = True      # --keep-pets 時關掉：共享寵物倉庫滿了（10 格），隊員的多餘寵物只能留在身上


def run_step(session: Session, map_dir: Path, task: int, step: int, *, say=print, script_dir: Path = SCRIPT_DIR,
             fight=fight, rounds: int = 0) -> Progress:
    """做任務的一步；做完確認伺服器的進度真的往前，否則停止。"""
    world = session.world
    keep_pets = task_pets(task, script_dir)
    if (STORE_SPARE_PETS and not session.battle.active and len(world.pets) > 2
            and world.mount_pet_index in world.pets and world.battle_pet_index in world.pets):
        # 使用者規定：解任務時身上只留騎寵與戰寵，其餘存共享寵物倉庫（任務獎勵常要空的寵物欄）。
        from .storage import store_spare_pets
        store_spare_pets(session, say=say, keep=keep_pets)
    if CARRY_ITEMS and not session.battle.active and world.items is not None:
        # 這個任務要帶在身上的道具（轉生要帶四大洞窟的證明）：缺了先從個人道具倉庫領，還是不齊就停，不帶著缺的去做。
        from .storage import withdraw_items
        withdraw_items(session, CARRY_ITEMS, say=say)
        missing = [name for name in CARRY_ITEMS if not world.has_item(name)]
        if missing:
            raise ProtocolError("要帶在身上的道具不齊：" + "、".join(missing) + "，已停止")
    if not session.battle.active:
        discard_listed(session, keep=unfinished_items(session, task, script_dir), say=say)
    if not session.battle.active and world.items is not None and world.free_bag_slots() < 4:
        # 背包快滿了：把這個任務用不到的道具（之前任務的獎勵）存共享倉庫，只留這個任務腳本提到的道具。
        from .storage import store_items
        store_items(session, task_items(task, script_dir) | set(CARRY_ITEMS), say=say)
    if not session.battle.active and world.items is not None and world.free_bag_slots() < 6:
        # 途中打怪掉的肉會把背包塞滿，NPC 要給任務道具時就收不下（2026-11-19 五兄弟的答案用紙）；每一步開始前清一次。
        # 紅暴任務交勾玉時空 3 格還是被說「道具已經滿了」，所以盡量清到 6 格（只丟肉，沒有肉可丟就算了）。
        make_room(session, 6, say=say)
    before = progress_of(session)
    if not FREE_ROUTE and before.next_step(task) != step:
        raise ProtocolError(f"任務 {task} 目前不是輪到第 {step} 步，已停止")
    path = script_dir / f"{task}-{step}.txt"
    if script_dir == SCRIPT_DIR and (OVERRIDE_DIR / path.name).is_file():
        path = OVERRIDE_DIR / path.name          # 實測修正過的腳本優先（見 data/quest_overrides/README.md）
    if not path.is_file():
        raise ProtocolError(f"找不到任務腳本 {path.name}，已停止")
    commands = parse_script(path.read_text(encoding="utf-8"))
    actions = plan_route(commands, has_item=session.world.has_item, has_pet=session.world.has_pet,
                        night=session.world.night(), rebirth=rebirth_of(session.world))
    actions = apply_choice(actions, load_choices().get(f"{task}-{step}"))
    team_fight = fight
    if not session.battle.active:
        # 提示是「购买…」的分支：這些任務道具在商城的石幣分頁（使用者 2026-11-19 告知並實測），
        # 把這一步腳本列的、身上還沒有的道具一次買齊，再照新的背包重排路線。
        notes = [action[1] for action in actions if action[0] == "note"]
        mall_items = load_mall_items()
        listed = [arguments[0] for name, arguments in commands
                  if name in ("I", "道具") and arguments and arguments[0] in mall_items]
        # 腳本沒有列道具的「购买…」是向那個 NPC 買（小豬的愛情故事第 2 步），照一般對話走，不去商城。
        if notes and notes[-1].startswith("购买") and any(not world.has_item(name) for name in listed):
            if rounds + 1 >= MAX_STEP_ROUNDS:
                raise ProtocolError(f"任務 {task} 第 {step} 步換分支次數超過上限，已停止")
            from .mall import buy_with_stone
            from .storage import store_items
            needed = listed
            lacking = sum(1 for name in set(needed) if not world.has_item(name))
            if world.free_bag_slots() < lacking:
                store_items(session, task_items(task, script_dir) | set(CARRY_ITEMS), say=say)      # 空位不夠就先把別的任務的道具存倉庫
            if world.free_bag_slots() < lacking:
                make_room(session, lacking, say=say)                             # 還不夠就丟肉
            buy_with_stone(session, needed, say=say)
            return run_step(session, map_dir, task, step, say=say, script_dir=script_dir, fight=team_fight,
                            rounds=rounds + 1)

    def fight(current):          # 既有的戰鬥流程只會打五人隊伍；隊伍不齊（單人）時改用逃跑
        if not solo_battle(current) and current.world.roles.ready():
            team_fight(current)
        else:
            say(f"有誰打誰，打了 {fight_present(current, say=say)} 回合")
    maps = [action[1] for action in actions if action[0] == "map"]
    hunting = None
    if maps and not any(action[0] == "talk" for action in actions):
        # 沒有對話的分支：如果提示是「原地遇敌击杀后获取…」，就走到那裡打怪直到拿到缺的道具，再重做這一步。
        missing = [arguments[0] for name, arguments in commands
                   if name in ("I", "道具") and arguments and not session.world.has_item(arguments[0])]
        if missing and any(action[0] == "note" and "遇敌" in action[1] for action in actions):   # 「原地遇敌击杀后获取…」「遇敌…获得…」
            hunting = missing[0]
    catching = None
    if maps and hunting is None and not any(action[0] == "talk" for action in actions):
        # 沒有對話、提示是「捕捉某某」的分支：走到那裡抓到那隻寵物，再重做這一步。
        notes = [action[1] for action in actions if action[0] == "note" and action[1].startswith("捕捉")]
        if notes and notes[-1][2:] in keep_pets and not session.world.has_pet(notes[-1][2:]):
            catching = notes[-1][2:]
    if not maps and any(action[0] == "talk" for action in actions) and not session.battle.active:
        # 分支只有對話、沒有地圖：缺道具時跳到的那一行落在別的分支中間（夢幻洞窟缺「先见之光」）。
        # 真實客戶端在這裡只會原地說一句話就結束（2026-11-19 擷取確認）；真正的補救路線是腳本的最後一段
        # （回去找給道具的 NPC）。照最後一段走完就回報目前進度，由外層決定接著做哪一步
        # （向老人空手說話會把任務退回上一步，那是預期中的）。
        ends = [number for number, (name, _) in enumerate(commands) if name in ("END", "结束")]
        tail = commands[ends[-2] + 1:] if len(ends) >= 2 else []
        fallback = plan_route(tail) if tail else []
        if not any(action[0] == "map" for action in fallback) or not any(action[0] == "talk" for action in fallback):
            raise ProtocolError("腳本這個分支只有對話，又沒有可以回去重拿道具的路線，已停止")
        say("缺任務道具：照腳本最後一段回去重拿")
        for number, action in enumerate(fallback):
            if session.battle.active:
                fight(session)
            after = next((later for later in fallback[number + 1:] if later[0] != "note"), None)
            if action[0] == "teleport":
                if session.world.map_id != RECORD_MAP:
                    session.return_to_record()
                say(f"傳送（代號 {teleport_code(action[1])}）→ {session.teleport(teleport_code(action[1]))}")
            elif action[0] == "map":
                wait_for_map(session, action[1], 5.0)
            elif action[0] == "walk":
                origin = session.world.map_id
                walk(session, map_dir, (action[1], action[2]), say=say, fight=fight)
                if after and after[0] == "map" and after[1] != origin and session.world.map_id == origin:
                    say(f"進入換圖點 → {enter_warp(session, map_dir, (action[1], action[2]), say=say, fight=fight)}")
            elif action[0] == "talk":
                for window in session.talk(action[1]):
                    say(f"［{window['title']}］{window['text']}".replace(NEWLINE_MARK, " "))
        end = time.monotonic() + 3
        while time.monotonic() < end:
            session.poll_event(0.3)
        return progress_of(session)
    if not maps and any(action[0] == "note" for action in actions):
        note = next(action[1] for action in actions if action[0] == "note")
        raise ProtocolError(f"腳本說要先做別的事：「{note}」，已停止")
    if not maps or (hunting is None and catching is None and not any(action[0] == "talk" for action in actions)):
        raise ProtocolError("腳本這個分支沒有對話（多半是要先回上一步重拿道具），已停止")
    first = resume_index(actions, session.world.map_id)
    talk_map = session.world.map_id
    collecting = any(name in ("I", "道具") for name, _ in commands)     # 這一步要靠收集道具來推進
    alone = False
    if first:
        say(f"已經在地圖 {session.world.map_id}，從路線中段接著走")
    skip_until = 0
    after_duel = False
    talk_override = None
    fought_boss = False
    if first:
        # 從中段接著走、而這張圖後面有守衛：如果已經站在守衛後面（跟著隊長過來的），不要走回去再打一次。
        # 判斷方式：守衛之後的第一個走路目標就在幾格內、而且直接走得到。
        duel_at = next((number for number in range(first, len(actions)) if actions[number][0] == "duel"), None)
        boundary = next((number for number in range(first + 1, len(actions)) if actions[number][0] == "map"), len(actions))
        if duel_at is not None and duel_at < boundary:
            beyond = next((number for number in range(duel_at + 1, boundary) if actions[number][0] == "walk"), None)
            if beyond is not None and not session.battle.active:
                target = (actions[beyond][1], actions[beyond][2])
                after = actions[beyond + 1] if beyond + 1 < len(actions) else ("",)
                duel_tile = (actions[duel_at][1], actions[duel_at][2])
                on_far_side = (abs(target[0] - session.world.x) + abs(target[1] - session.world.y)
                               < abs(target[0] - duel_tile[0]) + abs(target[1] - duel_tile[1]))
                if on_far_side and after[0] == "talk":
                    facing = reach_talk_spot(session, target, after[1], say=say)
                    if facing is not None:
                        say("已經在守衛後面，不必再打")
                        talk_override = facing
                        skip_until = beyond + 1
                elif on_far_side and max(abs(target[0] - session.world.x),
                                         abs(target[1] - session.world.y)) <= BLIND_WALK_DISTANCE:
                    try:
                        blind_walk(session, target, say=say)
                    except ProtocolError:
                        if session.sock is None:
                            raise
                    if (session.world.x, session.world.y) == target:
                        say("已經在守衛後面，不必再打")
                        skip_until = beyond + 1
                if on_far_side and not skip_until:
                    raise ProtocolError("人已經在守衛後面，卻走不到下一個目標；不走回去重打，已停止")
    for index, action in enumerate(actions):
        if index < max(first, skip_until):
            continue
        if session.battle.active:
            fight(session)
        if not session.battle.active and session.world.items is not None and session.world.free_bag_slots() < 3:
            make_room(session, 3, say=say)          # 途中打怪掉的肉隨時清，背包才收得下任務道具
        following = next((later for later in actions[index + 1:] if later[0] != "note"), None)   # 提示不算下一步
        if action[0] == "duel":
            # 走到目標旁邊（四個相鄰格由近到遠試），再當成「朝牠說話」處理；開打與打完的流程和一般對話相同。
            if f"{task}-{step}" in load_rallies():
                # 集合點的首領：走近的途中就可能被牠攔下開打，所以單人在動身之前就停。
                session.request_roles()
                if not any(role.status == 2 for role in session.world.roles.roles.values()):
                    raise ProtocolError(f"已到首領前（{action[3]}），這一場要全隊一起打，停在這裡等集合")
            target = (action[1], action[2])
            # 守衛站在通道的事件格上：要「站到牠那一格、朝接下來要去的方向說話」開打，打贏才算通過
            # （2026-11-19 拉多拉山頂的奇寶：站在南邊一格打贏三次，牠照樣擋路）。
            ahead = next((later for later in actions[index + 1:] if later[0] in ("walk", "talk", "map")), None)
            onward = ahead if ahead is not None and ahead[0] == "walk" else None
            direction = ahead[1] if ahead is not None and ahead[0] == "talk" else 0
            if onward is not None:
                dx, dy = onward[1] - target[0], onward[2] - target[1]
                direction = (2 if dx > 0 else 6) if abs(dx) > abs(dy) else (4 if dy > 0 else 0)
            try:
                if (session.world.x, session.world.y) != target:
                    walk(session, map_dir, target, say=say, fight=fight)
            except ProtocolError:
                if session.sock is None:
                    raise
            if (session.world.x, session.world.y) != target and max(
                    abs(target[0] - session.world.x), abs(target[1] - session.world.y)) <= BLIND_WALK_DISTANCE:
                blind_walk(session, target, say=say)
            if (session.world.x, session.world.y) != target:
                raise ProtocolError(f"走不到「{action[3]}」站的那一格 {target}，已停止")
            say(f"站到「{action[3]}」那一格 {target}，朝方向 {direction} 說話")
            action = ("talk", direction)
            after_duel = True
        if action[0] == "teleport":
            if session.world.map_id == maps[0]:
                continue
            if session.world.map_id != RECORD_MAP:
                session.return_to_record()
            say(f"傳送（代號 {teleport_code(action[1])}）→ {session.teleport(teleport_code(action[1]))}")
            landed = resume_index(actions, session.world.map_id)
            if session.world.map_id != maps[0] and landed > index:
                # 傳送直接落在路線後段的地圖（卡坦的願望：代號 138 直接到海底通路 10203，腳本卻從北島 200 寫起）。
                say(f"傳送落在地圖 {session.world.map_id}，從路線這一段接著走")
                first = landed
            elif session.world.map_id not in maps and catching is not None:
                # 抓寵的分支，傳送卻落在腳本沒寫到的地圖（卡坦的願望第 3 步：代號 139 落在 10201，
                # 加克拉就在那裡，2026-11-19 一號手動實測）：直接在落點抓。
                say(f"傳送落在腳本沒寫到的地圖 {session.world.map_id}，就在這裡抓「{catching}」")
                break
        elif action[0] == "map":
            wait_for_map(session, action[1], 5.0)
        elif action[0] == "walk":
            origin = session.world.map_id
            if (following and following[0] == "duel"
                    and max(abs(session.world.x - following[1]), abs(session.world.y - following[2])) <= 2):
                continue          # 人已經在守衛旁邊：不走回牠前面那一格（會把剛打贏的通過機會浪費掉）
            try:
                if after_duel and max(abs(action[1] - session.world.x),
                                      abs(action[2] - session.world.y)) <= BLIND_WALK_DISTANCE:
                    # 剛打贏擋路的守衛：牠站的那一格在地圖資料上仍是事件格，照地圖找路會繞一大圈
                    # （2026-11-19 拉多拉山頂的奇寶，繞了八分鐘）。下一個目標就在幾格內，直接走過去。
                    after_duel = False
                    if following and following[0] == "talk":
                        facing = reach_talk_spot(session, (action[1], action[2]), following[1], say=say)
                        if facing is None:
                            raise ProtocolError(f"打贏守衛後走不到 {(action[1], action[2])} 附近，已停止（不繞路、不走回頭）")
                        talk_override = facing
                        say(f"走到地圖 {origin} {(session.world.x, session.world.y)}")
                        continue
                    blind_walk(session, (action[1], action[2]), say=say)      # 走不過去就停，不照地圖繞遠路
                after_duel = False
                walk(session, map_dir, (action[1], action[2]), say=say, fight=fight)
            except ProtocolError as error:
                here = (session.world.x, session.world.y)
                earlier = actions[index - 1] if index else ("",)
                if ("走不進去" in str(error) and session.sock is not None and following
                        and following[0] == "talk" and not session.battle.active
                        and max(abs(action[1] - here[0]), abs(action[2] - here[1])) <= 2):
                    # 說話位置被隊友站住、自己就在旁邊一兩格：照樣說話試試（沒回應會在說話那一步停下）。
                    say(f"{(action[1], action[2])} 走不進去（被佔住），就在旁邊，直接說話")
                    continue
                if (following and following[0] == "duel" and session.sock is not None
                        and max(abs(following[1] - here[0]), abs(following[2] - here[1])) <= 2):
                    # 守衛前面那一格走不進去（隊友站著，或自己已經在守衛那一格上）：人就在守衛旁邊，直接處理守衛。
                    say(f"{(action[1], action[2])} 走不進去，人已經在守衛旁邊，直接處理守衛")
                    continue
                if ("通行路線" in str(error) and session.sock is not None and not session.battle.active
                        and session.world.map_id == origin
                        and max(abs(action[1] - here[0]), abs(action[2] - here[1])) <= BLIND_WALK_DISTANCE):
                    blind_walk(session, (action[1], action[2]), say=say)
                    if (session.world.x, session.world.y) == (action[1], action[2]):
                        say(f"走到地圖 {origin} {(action[1], action[2])}")
                        continue
                prior = next((actions[number] for number in range(index - 1, -1, -1)
                              if actions[number][0] not in ("note", "arrive")), ("",))
                if prior[0] == "walk" and earlier[0] == "arrive":
                    earlier = prior          # 腳本在傳送格後面寫了「到達」：一樣算是上一個走路目標（玄黃洞窟 11206 的 (5,38)）
                if ("通行路線" in str(error) and session.sock is not None and not session.battle.active
                        and earlier[0] == "walk" and tuple(earlier[1:]) == here):
                    # 上一個走路目標是同一張圖裡的傳送格，站上去卻沒被傳走（五兄弟領獎室 31706 的 (15,60)，
                    # 2026-11-19 三號實測）：在這一格送一次進入指令，位置變了就照原目標再走。
                    say(f"{here} 走不到下一個目標，試著從這一格傳送")
                    session._send_command(3, (3, 0, here[0], here[1], -1), time.monotonic() + session.timeout)
                    end = time.monotonic() + WARP_WAIT_SECONDS
                    while time.monotonic() < end and (session.world.x, session.world.y) == here:
                        session.poll_event(0.3)
                    if (session.world.x, session.world.y) != here:
                        session.sync()
                        say(f"被傳到 {(session.world.x, session.world.y)}")
                        walk(session, map_dir, (action[1], action[2]), say=say, fight=fight)
                        say(f"走到地圖 {origin} {(action[1], action[2])}")
                        if following and following[0] == "map" and following[1] != origin and session.world.map_id == origin:
                            say(f"進入換圖點 → {enter_warp(session, map_dir, (action[1], action[2]), say=say, fight=fight)}")
                        continue
                # 從中段接著走時，同一張圖的前一段可能已經做過、而且回不去（打贏守衛後被傳到另一側；
                # 2026-11-19 強恩一族洞窟 20607 實測）。同一張圖後面還有走路目標就直接從那一段接。
                later = next((later_index for later_index in range(index + 1, len(actions))
                              if actions[later_index][0] in ("walk", "map")), None)
                if not (first and "通行路線" in str(error) and session.sock is not None
                        and later is not None and actions[later][0] == "walk"):
                    raise
                say(f"{(action[1], action[2])} 走不到，改從同一張圖的下一段 {actions[later][1:]} 接著走")
                skip_until = later
                continue
            say(f"走到地圖 {origin} {(action[1], action[2])}")
            if following and following[0] == "map" and following[1] != origin and session.world.map_id == origin:
                say(f"進入換圖點 → {enter_warp(session, map_dir, (action[1], action[2]), say=say, fight=fight)}")
        elif action[0] == "talk":
            if talk_override is not None:
                action, talk_override = ("talk", talk_override), None
            talk_map = session.world.map_id
            earlier = actions[index - 1] if index else ("", "")
            rally = f"{task}-{step}" in load_rallies() and earlier[0] == "note" and "击败" in earlier[1]
            if rally:
                session.request_roles()
                if not any(role.status == 2 for role in session.world.roles.roles.values()):
                    raise ProtocolError(f"已到首領前（{earlier[1]}），這一場要全隊一起打，停在這裡等集合")
                say("全隊到齊，一起打首領")
            elif collecting and not alone and not TEAM_MODE and earlier[0] == "note" and "击败" in earlier[1]:
                # 打首領拿任務道具：戰利品是全隊分配，帶隊打贏時道具會落到隊員身上（2026-11-18 強盜洞窟實測，
                # 帶隊打贏怪力小馬兩次都沒拿到「萨顿家的衣服」，單人打一次就拿到）。所以開打前先離隊。
                say("要打首領拿任務道具，先離隊（戰利品是全隊分配）")
                leave_party(session, say=say)
                alone = True
            seen = session.talk(action[1])
            if not seen and not session.battle.active and session.world.map_id == talk_map:
                # 實測（龍洞守護者）：剛走到就說話偶爾沒有回應，隔幾秒再說一次就有；只補這一次。
                time.sleep(3.0)
                seen = session.talk(action[1])
            if session.battle.active:
                say("對話後進入戰鬥（首領戰）")
                fought_boss = True
                # 車隊模式是全隊打：隊長血少也不逃（一逃整場就白打，奇寶第一次就是這樣沒過）。
                say(f"首領戰打了 {fight_present(session, say=say, allow_escape=not TEAM_MODE)} 回合")
            elif following and following[0] == "arrive" and (
                    session.world.x, session.world.y) != (following[2], following[3]) and (
                    session.world.map_id != following[1] or talk_map == following[1]):
                # 同一張圖內的傳送也算（碧青洞窟賢者的看門人：帶著隊伍說話只開一個空視窗、人沒被傳進院子）。
                # 把人傳進任務場地的 NPC 有些只收單人（成人儀式入口對五人隊伍只說「无法参加宴会」）：離隊再試一次。
                for window in seen:
                    say(f"［{window['title']}］{window['text']}".replace(NEWLINE_MARK, " "))
                say("NPC 沒有傳送，離隊後再試一次")
                leave_party(session, say=say)
                seen = session.talk(action[1])
            # 對話後接著是「被傳到某處」時，NPC 可能不開視窗直接傳送。
            if not seen and after_duel and not session.battle.active:
                # 守衛沒有回應：隔幾秒再說一次（只補這一次）；還是沒有就停，不硬闖。
                time.sleep(5.0)
                seen = session.talk(action[1])
                if session.battle.active:
                    say("對話後進入戰鬥（首領戰）")
                    say(f"首領戰打了 {fight_present(session, say=say, allow_escape=not TEAM_MODE)} 回合")
                elif not seen:
                    # 守衛不理人＝這隻角色已經打贏過牠（2026-11-19 奇寶：同一時間牠不理打贏過的二號，
                    # 卻照常回應沒打過的三號）。不重打：試著直接往前走；走不過去就停，交給隊友帶。
                    # 也可能是自己其實還沒站上守衛那一格（位置差一格，對到的是不理人的守衛本身）：
                    # 朝說話的方向再走一步、再說一次，最多兩次（2026-11-19 寶卡：二號以為在 (344,356)，其實在 (343,356)）。
                    if action[1] in FACING:
                        for _ in range(2):
                            here = (session.world.x, session.world.y)
                            session._send_command(1, (here[0], here[1], STEP_LETTERS[FACING[action[1]]]),
                                                  time.monotonic() + session.timeout)
                            session.sync()
                            time.sleep(1.5)
                            seen = session.talk(action[1])
                            if seen or session.battle.active:
                                break
                    if session.battle.active:
                        say("對話後進入戰鬥（首領戰）")
                        fought_boss = True
                        say(f"首領戰打了 {fight_present(session, say=say, allow_escape=not TEAM_MODE)} 回合")
                    elif seen:
                        say("往前一步後有人回話了")
                    else:
                        say("守衛不理這隻角色（已經打贏過），直接往下走")
                    after_duel = False
                    if not seen and not session.battle.active:
                        continue
            if not seen and not (following and following[0] in ("arrive", "note")):
                if progress_of(session).next_step(task) != step:
                    say("這一步的進度已經前進了，後面的對話不必再說")
                    break
                raise ProtocolError("NPC 沒有回應，已停止")
            for window in seen:
                say(f"［{window['title']}］{window['text']}".replace(NEWLINE_MARK, " "))
        elif action[0] == "arrive":
            end = time.monotonic() + 10.0
            while session.world.map_id == talk_map != action[1] and time.monotonic() < end:
                session.poll_event(0.3)
            if session.world.map_id not in (talk_map, action[1]):
                # 迷宮入口實際傳到的樓層和腳本寫的不一定相同（馬祖任務直接到了最後一層）：
                # 落點如果是路線後面會經過的地圖，就從那裡接著走。
                later = [j for j in range(index + 1, len(actions)) if actions[j] == ("map", session.world.map_id)]
                if not later:
                    raise ProtocolError(f"被傳到路線上沒有的地圖 {session.world.map_id}，已停止")
                say(f"被傳到地圖 {session.world.map_id}（腳本寫的是 {action[1]}），從這張圖接著走")
                first = later[-1]
                continue
            wait_for_map(session, action[1])
            end = time.monotonic() + 5.0
            while time.monotonic() < end and (session.world.x, session.world.y) != (action[2], action[3]):
                session.poll_event(0.3)
            say(f"被傳到地圖 {session.world.map_id} ({session.world.x},{session.world.y})")
        elif action[0] == "note":
            say(f"腳本提示：{action[1]}")
    if hunting is not None:
        hunt(session, hunting, say=say)
        return run_step(session, map_dir, task, step, say=say, script_dir=script_dir, fight=team_fight)
    if catching is not None:
        capture(session, catching, say=say)
        if STOP_AFTER_CAPTURE:
            say(f"抓到「{catching}」，照設定先停在這裡，不接著做下一段")
            return progress_of(session)
        if rounds + 1 >= MAX_STEP_ROUNDS:
            raise ProtocolError(f"任務 {task} 第 {step} 步換分支次數超過上限，已停止")
        return run_step(session, map_dir, task, step, say=say, script_dir=script_dir, fight=team_fight,
                        rounds=rounds + 1)
    end = time.monotonic() + 3
    while time.monotonic() < end:
        session.poll_event(0.3)
    after = progress_of(session)
    if after.next_step(task) == step:
        # 同一步裡要依序拿好幾樣道具的任務（強盜洞窟：打三個首領各拿一件神器）：進度不會前進，
        # 但背包變了，腳本的 `I` 會跳到下一條分支。路線真的變了才接著做，而且有次數上限。
        again = apply_choice(plan_route(commands, has_item=session.world.has_item, has_pet=session.world.has_pet,
                        night=session.world.night(), rebirth=rebirth_of(session.world)),
                             load_choices().get(f"{task}-{step}"))
        if again == actions and fought_boss and rounds + 1 < 2:
            # 這一步的對話引發了首領戰：打贏之後要再對同一個位置說一次話，NPC 才給東西
            # （玄黃洞窟第 2 步：先打跑盜賊寶卡，再跟被救的哈姆薩說話）。重做一次，只重做這一次。
            say("打完首領後進度沒有前進，再做一次這一步（打贏後要再說一次話）")
            return run_step(session, map_dir, task, step, say=say, script_dir=script_dir, fight=team_fight,
                            rounds=rounds + 1)
        if again == actions or rounds + 1 >= MAX_STEP_ROUNDS:
            raise ProtocolError(f"對話結束但任務 {task} 的進度沒有前進，已停止")
        say(f"任務 {task} 第 {step} 步還沒結束，背包變了，照腳本的下一條分支接著做")
        return run_step(session, map_dir, task, step, say=say, script_dir=script_dir, fight=team_fight,
                        rounds=rounds + 1)
    return after


def _item_changed(session: Session, slot: int, before, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end and session.world.items.get(slot) == before:
        session.poll_event(0.2)
    return session.world.items.get(slot) != before


def _send_item(session: Session, func: int, values: tuple) -> None:
    try:
        session._send_command(func, values, time.monotonic() + session.timeout)
    except BaseException:
        session.close()
        raise


DISCARD_FILE = Path(__file__).resolve().parent.parent / "data" / "discard_items.json"
MAX_DISCARDS = 20


def load_discards(path: Path = DISCARD_FILE) -> tuple:
    try:
        names = json.loads(path.read_text(encoding="utf-8"))["丟棄"]
    except (OSError, ValueError, KeyError):
        return ()
    return tuple(name for name in names if isinstance(name, str) and name)


def unfinished_items(session: Session, task=None, script_dir: Path = SCRIPT_DIR) -> set:
    """還不能丟的道具：正要做的任務、以及所有做到一半（還沒完成）的任務的腳本提到的道具。
    使用者 2026-11-19 的規定：名單上的道具要等對應任務確定完成才丟。"""
    progress = progress_of(session)
    names = set()
    for number in set(progress.steps) | ({task} if task is not None else set()):
        if number not in progress.done:
            # 只看還沒做的步驟：做過的步驟用過的道具（紅暴任務前幾步的貝殼）已經用不到了。
            names |= task_items(number, script_dir, first_step=progress.steps.get(number, 0) + 1)
    return names


def discard_listed(session: Session, *, keep=(), say=print, names=None) -> list:
    """把背包裡列在 data/discard_items.json 的道具丟到地上（C19，一次一個）；keep 裡的不丟
    （呼叫端給 unfinished_items()：還沒完成的任務會用到的都留著）。

    每丟一個都等伺服器回報該格變化；伺服器不受理就停，不改用別的方式。回傳丟掉的名稱。"""
    world = session.world
    names = load_discards() if names is None else names
    if not names or world.items is None or session.battle.active:
        return []
    dropped = []
    for slot in sorted(world.items):
        if not 9 <= slot <= 23 or world.items[slot][0] not in names or world.items[slot][0] in keep:
            continue
        name = world.items[slot][0]
        while slot in world.items:
            if len(dropped) >= MAX_DISCARDS:
                raise ProtocolError("一次丟棄的道具數超過上限，已停止")
            before = world.items[slot]
            time.sleep(EAT_SECONDS)
            _send_item(session, 19, (world.x, world.y, slot))
            if not _item_changed(session, slot, before, 3.0):
                raise ProtocolError(f"丟棄「{name}」伺服器沒有受理，已停止")
            dropped.append(name)
    if dropped:
        say("照丟棄名單丟掉：" + "、".join(dropped))
    return dropped


MAX_PET_DISCARDS = 5


def load_pet_discards(path: Path = DISCARD_FILE) -> tuple:
    """丟棄名單上的寵物：（名稱…, 等級上限）。讀不到就是不丟。"""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))["丟棄寵物"]
        names = tuple(name for name in entry["名稱"] if isinstance(name, str) and name)
        return names, int(entry["等級上限"])
    except (OSError, ValueError, KeyError, TypeError):
        return (), 0


def discard_pets(session: Session, *, say=print, names=None, maximum_level=None) -> list:
    """把寵物欄裡列在丟棄名單（data/discard_items.json 的「丟棄寵物」）的寵物丟到地上（C21，不可逆）。
    騎寵、戰寵不丟；等級高於上限的不丟。每丟一隻都等伺服器回報該格消失，不受理就停。回傳丟掉的名稱。"""
    world = session.world
    listed, cap = load_pet_discards()
    names = listed if names is None else names
    maximum_level = cap if maximum_level is None else maximum_level
    if not names or session.battle.active:
        return []
    dropped = []
    for index in sorted(world.pets):
        pet = world.pets[index]
        name = pet.name.decode("gbk", "replace")
        if (name not in names or index in (world.mount_pet_index, world.battle_pet_index)
                or pet.level is None or pet.level > maximum_level):
            continue
        if len(dropped) >= MAX_PET_DISCARDS:
            raise ProtocolError("一次丟棄的寵物數超過上限，已停止")
        time.sleep(1.0)
        _send_item(session, 21, (world.x, world.y, index))
        end = time.monotonic() + 4.0
        while time.monotonic() < end and index in world.pets:
            session.poll_event(0.2)
        if index in world.pets:
            raise ProtocolError(f"丟棄寵物「{name}」伺服器沒有受理，已停止")
        dropped.append(name)
    if dropped:
        say("照丟棄名單丟掉寵物：" + "、".join(dropped))
    return dropped


def make_room(session: Session, wanted: int, *, say=print) -> None:
    """背包空位不到 wanted 格時清掉打怪掉的肉（名稱以「肉」結尾），其他道具不碰。

    做的事和遊戲外掛面板的「丟棄或吃掉任何種類肉」一樣——那個設定存在客戶端本機，由客戶端自己送封包，
    無畫面登入沒有它，所以這裡自己做：先整格丟到地上（C19）；伺服器不受理（腳下或周圍已經放滿東西）
    就改成一個一個吃掉（C17，血滿也會消耗）。每個動作都等伺服器回報該格變化才算數。"""
    world = session.world
    refused = 0
    for slot in sorted(world.items or {}, reverse=True):
        if world.free_bag_slots() >= wanted:
            return
        name, count = world.items[slot]
        if not (9 <= slot <= 23 and name.endswith("肉")):
            continue
        removed = 0
        for _ in range(MAX_STACK):
            before = world.items.get(slot)
            if before is None:
                break
            time.sleep(EAT_SECONDS)
            # 2026-11-18 實測：C19 一次只丟一個（堆疊數減 1）；不受理時改用 C17 吃掉一個。
            _send_item(session, 19, (world.x, world.y, slot))
            if _item_changed(session, slot, before, 2.0):
                removed += 1
                continue
            _send_item(session, 17, (world.x, world.y, slot, 0))
            if _item_changed(session, slot, before, 3.0):
                removed += 1
                continue
            break
        if slot in world.items:
            say(f"背包第 {slot} 格的{name}清了 {removed} 個後清不動，跳過")
            refused += 1
            if refused >= 3:
                break
        else:
            say(f"清掉背包第 {slot} 格的{name}（{removed} 個）")
    if world.free_bag_slots() < min(wanted, 1):
        raise ProtocolError("背包沒有空位又清不掉，已停止")


def load_tasks(path: Path = TASKS_FILE) -> dict:
    return {task["id"]: task for task in json.loads(path.read_text(encoding="utf-8"))}


def run_with_party(config: dict, map_dir: Path, task: int) -> int:
    """借用練功總控的連線、組隊與戰鬥流程，只做一個任務就結束（不練功）。"""
    from .supervisor import Supervisor
    if map_dir is None or task is None:
        raise ValueError("--party 需要 --map-dir 與 --task")
    boss = Supervisor(config, map_dir=map_dir, target_level=200, maximum_seconds=3600.0)
    with boss.connect() as session:
        session.sync()
        # 登入當下若還卡在戰鬥裡，要查過隊伍名冊後伺服器才會補送戰鬥資料（實測）；先把那一場打完。
        session.request_roles()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            session.poll_event(0.3)
        if session.battle.active:
            boss.say("登入時還在戰鬥中，先打完")
            fight_present(session, say=boss.say)
        if not session.battle.active and session.world.map_id not in (RECORD_MAP, 500):
            # 上次停在任務場地或迷宮裡：先回記錄點再組隊（在儀式場地裡召喚隊員會逾時）。
            boss.say(f"目前在地圖 {session.world.map_id}，先回記錄點")
            session.return_to_record()
        boss.party(session)
        if session.battle.active:
            boss.finish_battle(session)
        boss.run_quest(session, task)
    return 0


def run_with_team(config: dict, map_dir: Path, task: int, team: tuple, *, stay: bool = False) -> int:
    """指定的幾隻角色自己組一隊去做任務：第一隻當隊長登入並操作，其餘跟隨（使用者 2026-11-19 要三、四、五號先組隊解）。

    只有隊長的任務進度會前進（NPC 對話算在說話的人身上）。戰鬥一律用「有誰打誰」。
    隊員離線時 ensure_party 會送一鍵登入（AL），不在這一隊的離線角色也會被叫上線但不組隊。
    2026-11-19 實測：隊員還掛在別的連線名下（名冊狀態 3）時，這裡下的召喚／組隊指令伺服器不回應，
    要先讓它們各自單獨登入一次、等幾分鐘變成離線再來。"""
    from .supervisor import Supervisor
    leader, members = team[0], tuple(team[1:])
    if map_dir is None or task is None or not members or len(set(team)) != len(team):
        raise ValueError("--team 需要 --map-dir、--task，以及至少兩隻不重複的角色")
    tasks = load_tasks()
    if task not in tasks:
        raise ValueError("任務編號不在任務表內")
    say = lambda message: print(f"{time.strftime('%H:%M:%S')} [隊長 {leader}] {message}", flush=True)
    with Session(timeout=20) as session:
        session.authenticate(config["host"], int(config["port"]), config["account"], str(leader),
                             config["subaccounts"][str(leader)]["password_md5"],
                             server_index=int(config["server_index"]))
        session.request_characters()
        session.select_character()
        session.read_initial_world()
        session.sync()
        roles = session.request_roles()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            session.poll_event(0.3)
        if session.battle.active:
            say("登入時還在戰鬥中，先打完")
            fight_present(session, say=say)
        say("名冊：" + "、".join(f"{role.slot}:{role.status}" for role in roles))
        if session.world.roles.ready(leader, members):
            say("隊伍已經組好，從目前位置接著做")        # 帶著任務道具時伺服器不准回記錄點（五兄弟的答案用紙實測）
        elif stay:
            say(f"在地圖 {session.world.map_id} 原地組隊（集合點）")
        elif session.world.map_id not in (RECORD_MAP, 500):
            say(f"目前在地圖 {session.world.map_id}，先回記錄點")
            session.return_to_record()
        session.ensure_party(leader=leader, members=members)
        Supervisor(config, map_dir=map_dir, target_level=200, maximum_seconds=3600.0).use_plan(session.world.map_id or 0)
        session.ensure_pets(slots=team)
        say("隊伍：" + "、".join(f"{slot}:{session.world.roles.roles[slot].status}" for slot in team))
        progress = progress_of(session)
        make_room(session, 3, say=say)
        for _ in range(len(tasks[task]["steps"])):
            step = progress.next_step(task)
            if step is None:
                break
            say(f"任務 {task} {tasks[task]['title']}：第 {step}／{len(tasks[task]['steps'])} 步")
            progress = run_step(session, map_dir, task, step, say=say,
                                fight=lambda current: say(f"有誰打誰，打了 {fight_present(current, say=say)} 回合"))
        say(f"任務 {task}：" + ("已完成" if task in progress.done else "尚未完成"))
    return 0


TEAM_MODE = False            # 車隊模式：首領一律全隊打，不為了拿任務道具而離隊
MAX_CARAVAN_TURNS = 120


STEP_LETTERS = {(0, -1): b"a", (1, -1): b"b", (1, 0): b"c", (1, 1): b"d",
                (0, 1): b"e", (-1, 1): b"f", (-1, 0): b"g", (-1, -1): b"h"}
BLIND_WALK_DISTANCE = 8


def blind_walk(session: Session, goal: tuple, *, say=print, trust=False) -> None:
    """地圖資料說走不通、但目的地就在幾格之內時，直接朝目的地一步一步走，由伺服器決定能不能走。

    2026-11-19 達那村長老房間（5106）：被召喚的隊員站在伺服器地圖標成「虛空＋隱形牆」的區域裡
    （實際站得上去、也走得出來），照地圖找路永遠找不到。只在距離 8 格以內用，走不動兩次就停。"""
    world = session.world
    if max(abs(goal[0] - world.x), abs(goal[1] - world.y)) > BLIND_WALK_DISTANCE:
        raise ProtocolError("離目的地太遠，不摸黑走，已停止")
    stuck = 0
    try:
        for _ in range(BLIND_WALK_DISTANCE * 3):
            here = (world.x, world.y)
            if here == tuple(goal):
                say(f"摸黑走到 {here}")
                return
            if session.battle.active:
                return
            delta = ((goal[0] > here[0]) - (goal[0] < here[0]), (goal[1] > here[1]) - (goal[1] < here[1]))
            options = [delta] + [d for d in ((delta[0], 0), (0, delta[1])) if d != (0, 0) and d != delta]
            moved = False
            for option in options:
                world.x = world.y = -1                 # 記號：等這一步的位置回報（和 walk() 同樣的做法）
                session._send_command(1, (here[0], here[1], STEP_LETTERS[option]), time.monotonic() + session.timeout)
                session.sync()
                end = time.monotonic() + 3.0
                while time.monotonic() < end and (world.x, world.y) == (-1, -1):
                    session.poll_event(0.2)
                if (world.x, world.y) == (-1, -1):
                    # 沒有位置回報。角色和隊友疊在一起時伺服器常常不回報（2026-11-19 拉多拉山頂：四號其實走得動，
                    # 我卻一直以為沒動）。trust=True 時照推算的位置往下走，由之後的對話有沒有回應來驗證。
                    world.x, world.y = (here[0] + option[0], here[1] + option[1]) if trust else here
                if (world.x, world.y) != here:
                    moved = True
                    break
            stuck = 0 if moved else stuck + 1
            if stuck >= 3:
                break
    except BaseException:
        session.close()
        raise
    raise ProtocolError(f"摸黑也走不到 {tuple(goal)}（停在 {world.x},{world.y}），已停止")


FACING = {0: (0, -1), 2: (1, 0), 4: (0, 1), 6: (-1, 0)}


def reach_talk_spot(session: Session, goal: tuple, direction: int, *, say=print):
    """走到 goal（要朝 direction 對 NPC 說話的位置）；goal 被別的角色站住走不進去時，改站 NPC 的另外三側。
    回傳實際要用的說話方向；都走不到回 None。只用在幾格內的短距離（摸黑走）。

    2026-11-19 拉多拉山頂：先拿到羽毛的一號就站在不死鳥正前方 (32,29)，後面的角色打贏奇寶後走不進那一格
    （這個遊戲角色會擋路），被我的程式誤判成過不了守衛。"""
    world = session.world
    if direction not in FACING:
        return None
    npc = (goal[0] + FACING[direction][0], goal[1] + FACING[direction][1])
    spots = [(tuple(goal), direction)] + [((npc[0] - FACING[d][0], npc[1] - FACING[d][1]), d)
                                          for d in FACING if d != direction]
    for spot, facing in spots:
        if max(abs(spot[0] - world.x), abs(spot[1] - world.y)) > BLIND_WALK_DISTANCE:
            continue
        try:
            if (world.x, world.y) != spot:
                blind_walk(session, spot, say=lambda message: None, trust=True)
        except ProtocolError:
            if session.sock is None:
                raise
            continue
        if (world.x, world.y) == spot:
            if spot != tuple(goal):
                say(f"{tuple(goal)} 被佔住，改站 {spot} 朝方向 {facing} 說話")
            return facing
    return None


def script_dir_for(task: int, step: int, script_dir: Path = SCRIPT_DIR) -> Path:
    """這一步實際要用的腳本檔（修正版優先）。"""
    name = f"{task}-{step}.txt"
    return OVERRIDE_DIR / name if (OVERRIDE_DIR / name).is_file() else script_dir / name


def handoff(session: Session, to: int) -> None:
    """把五控的操作權（也就是隊長）交給另一隻角色：`C2025 "Q|編號"`，伺服器回 `S2024 "Q|編號"`。

    2026-11-19 用真實客戶端的「切換角色」按鈕擷取：送出後隊伍原封不動改由那一隻帶隊，
    客戶端接著用那一隻重新連線。不必等名冊變成離線（另開連線去召喚才需要等 4～5 分鐘）。"""
    try:
        session._send_command(2025, (b"Q|%d" % to,), time.monotonic() + session.timeout)
        end = time.monotonic() + session.timeout
        while time.monotonic() < end:
            event = session.poll_event(0.3)
            if event is not None and event.func == 2024 and event.values[0].startswith(b"Q|%d" % to):
                return
    except BaseException:
        session.close()
        raise
    raise ProtocolError(f"把操作權交給角色 {to} 沒有收到確認，已停止")


def reform(session: Session, leader: int, members: tuple, *, say=print, summon=None) -> None:
    """讓目前連線的這一隻成為真正的隊長：其餘各隻先下線（`O|編號`，同時會離開舊隊伍），再一鍵登入（`AL`）、
    一鍵召喚（`AZ`，拉到隊長身邊）、一鍵組隊（`AT`）。2026-11-19 實測全程十幾秒，不必等名冊變離線。

    只送 `Q|編號` 換操作權是不夠的：遊戲裡的隊伍仍掛在舊隊長名下，新隊長傳送會被擋
    （「组队状态队友下无法传送」），再組隊也會被拒（「子账号已有队伍」）。"""
    roles = session.world.roles

    def control(command: bytes, done, seconds=25.0):
        try:
            session._send_command(2025, (command,), time.monotonic() + session.timeout)
            end = time.monotonic() + seconds
            while time.monotonic() < end and not done():
                session.poll_event(0.3)
        except BaseException:
            session.close()
            raise
        if not done():
            raise ProtocolError(f"五控指令 {command.decode()} 沒有等到結果，已停止")

    session.request_roles()
    time.sleep(2.0)
    for member in members:
        if roles.roles[member].status != 0:
            control(b"O|%d" % member, lambda member=member: roles.roles[member].status == 0)
            time.sleep(1.6)      # 伺服器規定下線操作之間要隔 1 秒以上（「请过1秒后再进行下线操作」）
    control(b"AL", lambda: all(roles.roles[member].status != 0 for member in members))
    time.sleep(1.5)
    session.request_roles()
    here = roles.roles[leader].map_name
    if summon is None:
        # 隊員已經和隊長在同一張圖（跟著走過來的）就不召喚：召喚會把隊員丟到隊長附近的格子，不檢查能不能站，
        # 在狹窄的房間裡會落進牆裡動彈不得（2026-11-19 達那村長老房間，五號、一號都卡過）。
        summon = any(roles.roles[member].map_name != here for member in members)
    summoned = set(members) if not summon else set()
    original = roles.apply

    def watch(message, original=original):
        result = original(message)
        if message.startswith(b"Z|"):
            summoned.add(int(message.split(b"|")[1]))
        return result
    roles.apply = watch
    try:
        # 召喚和組隊都逐隻下指令（Z|編號、T|編號），不用一鍵版：一鍵版會連不在這一隊、正在單獨連線的角色
        # 一起叫，伺服器會回「子账号真机在线，无法被召唤」（2026-11-19 三人車隊實測）。
        for member in members:
            if summon:
                control(b"Z|%d" % member, lambda member=member: member in summoned)
                time.sleep(1.6)      # 召喚之間也要隔 1 秒以上（「请过1秒后再进行召唤操作」）
    finally:
        roles.apply = original
    time.sleep(1.0)
    for member in members:
        control(b"T|%d" % member, lambda member=member: roles.roles[member].status == 2)
        time.sleep(1.2)
    say("隊伍重組完成：" + "、".join(f"{slot}:{roles.roles[slot].status}" for slot in (leader,) + tuple(members)))


CARAVAN_HEAL_BELOW = 85      # 車隊隊長血量低於這個百分比就用免費烤豬排補（任務模組可依任務調整）
CARAVAN_DISCARD = True       # 每一步前照丟棄名單清道具
STORE_AFTER_DONE = ()        # 角色做完這個任務後，要存進個人道具倉庫的道具名稱
CARAVAN_STOP_AFTER_FIRST = False   # 第一隻角色做完這個任務就停（轉生這種不可逆的任務用）
CARAVAN_FAST = False         # 每次連線後打開快戰開關（配合 FIGHTER）
CARAVAN_ON_CONNECT = None    # 每次連線後呼叫（角色, 連線）：任務模組用來更新面板快照
CARAVAN_PRE_HEAL = None     # 補血前先做的事（任務模組開了自動吃肉時＝先吃背包裡的肉）


def run_caravan(config: dict, map_dir: Path, task: int, team: tuple, controller: int | None = None) -> int:
    """車隊模式：全隊一起走，每一回合由「這個任務進度最落後的那一隻」當隊長做牠自己的下一步，
    做完用 handoff 換下一隻。同一個 NPC 前面，五隻輪流上前說話；首領每隻各打一次，都是全隊打。

    前提：隊伍已經組好（任一隻是隊長、其餘跟隨），或大家都離線可以由第一隻就地召喚。"""
    global TEAM_MODE
    if map_dir is None or task is None or len(team) < 2 or len(set(team)) != len(team):
        raise ValueError("--caravan 需要 --map-dir、--task，以及至少兩隻不重複的角色")
    tasks = load_tasks()
    if task not in tasks:
        raise ValueError("任務編號不在任務表內")
    total = len(tasks[task]["steps"])
    TEAM_MODE = True
    known = {}                   # 角色 → 下一步（None＝已完成）
    leader, formed = None, False
    rescued = set()
    relogged = set()
    ran = set()                  # 這一次執行裡實際做過步驟的角色
    stored_after = set()         # 做完後已經存過獎勵的角色

    def connect(sub: int) -> Session:
        session = Session(timeout=20)
        session.authenticate(config["host"], int(config["port"]), config["account"], str(sub),
                             config["subaccounts"][str(sub)]["password_md5"],
                             server_index=int(config["server_index"]))
        session.request_characters()
        session.select_character()
        session.read_initial_world()
        session.sync()
        session.request_roles()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            session.poll_event(0.3)
        if CARAVAN_ON_CONNECT is not None:
            CARAVAN_ON_CONNECT(sub, session)
        if CARAVAN_FAST and not session.battle.active:
            session._send_command(2025, (f"KS|{session.sock.fileno()}|1|".encode(),), time.monotonic() + 20)
        return session

    # controller：目前握有五控操作權的那一隻（上一次車隊沒有正常交接就結束時，隊員還掛在牠名下，
    # 別隻去下線會被拒：「子账号被[某某]控了控制，无法通过这里下线」）。先用牠連線，之後照 team 的順序輪。
    state_file = Path(__file__).resolve().parent.parent / "captures" / "caravan-leader.local.txt"
    if controller is None:
        try:
            controller = int(state_file.read_text(encoding="utf-8").strip())      # 上一次車隊結束時的隊長
        except (OSError, ValueError):
            controller = None
    start = controller if controller in team else team[0]
    session = connect(start)
    leader = start

    def remember(sub: int) -> None:
        try:
            state_file.write_text(str(sub), encoding="utf-8")
        except OSError:
            pass
    remember(start)
    try:
        # 開場先重組一次：交接操作權（Q）只對已經掛在這條連線名下的角色有效，
        # 隊員若剛被單獨登入過，Q 會沒有回應（2026-11-19 實測）。
        if session.battle.active:
            fight_present(session, say=print, allow_escape=False)
        reform(session, leader, tuple(sub for sub in team if sub != leader),
               say=lambda message: print(f"{time.strftime('%H:%M:%S')} [隊長 {start}] {message}", flush=True))
        formed = True
        for turn in range(MAX_CARAVAN_TURNS):
            say = lambda message, who=leader: print(f"{time.strftime('%H:%M:%S')} [隊長 {who}] {message}", flush=True)
            members = tuple(sub for sub in team if sub != leader)
            if session.battle.active:
                fight_present(session, say=say, allow_escape=False)
            progress = progress_of(session)
            known[leader] = progress.next_step(task)
            if STORE_AFTER_DONE and leader in ran and known[leader] is None and leader not in stored_after:
                # 這隻剛做完這個任務：把指定的獎勵存進牠自己的個人道具倉庫（只有牠當隊長的這條連線碰得到牠的背包）
                stored_after.add(leader)
                from .storage import store_named
                store_named(session, STORE_AFTER_DONE, say=say)
            if CARAVAN_STOP_AFTER_FIRST and leader in ran and known[leader] is None:
                # 不可逆的任務（轉生）：第一隻做完先停，讓人確認結果再決定其他隻
                print(f"任務 {task}：角色 {leader} 已完成，照設定先停下來，不接著做其他角色", flush=True)
                return 0
            pending = [sub for sub in team if sub not in known]
            if pending:
                target = pending[0]                       # 先把每一隻的進度都看過一遍
            else:
                waiting = {sub: step for sub, step in known.items() if step is not None}
                if not waiting:
                    print(f"任務 {task}：車隊全員已完成", flush=True)
                    return 0
                target = min(waiting, key=lambda sub: (waiting[sub], team.index(sub)))
            if target != leader:
                say(f"換角色 {target} 當隊長（各角色下一步：{known}）")
                handoff(session, target)
                session.close()
                time.sleep(2.0)
                session = connect(target)
                leader, formed = target, False
                remember(leader)
                continue
            step = known[leader]
            if not formed:
                reform(session, leader, members, say=say)
                formed = True
            # 這一步要從村裡傳送出發、而隊伍不在記錄點：隊長回記錄點時隊員不會跟著走，
            # 所以隊長自己先回去，再重組一次把隊員召喚到身邊。
            path = script_dir_for(task, step)
            first_actions = plan_route(parse_script(path.read_text(encoding="utf-8")),
                                       has_item=session.world.has_item, has_pet=session.world.has_pet,
                                       night=session.world.night(), rebirth=rebirth_of(session.world))
            route_maps = [action[1] for action in first_actions if action[0] == "map"]
            starts_with_teleport = bool(first_actions) and first_actions[0][0] == "teleport"
            roster = session.world.roles.roles
            scattered = len({roster[sub].map_name for sub in team if sub in roster}) > 1
            if (starts_with_teleport and session.world.map_id not in route_maps
                    and (session.world.map_id != RECORD_MAP or scattered)):
                say("下一步要從記錄點傳送出發：隊長先回記錄點，再把隊員召喚過來")
                if session.world.map_id != RECORD_MAP:
                    session.return_to_record()
                reform(session, leader, members, say=say, summon=True)
            if CARAVAN_PRE_HEAL is not None and solo_hp_percent(session.world) < CARAVAN_HEAL_BELOW:
                CARAVAN_PRE_HEAL(session, say)
            if solo_hp_percent(session.world) < CARAVAN_HEAL_BELOW:
                heal_with_items(session, say=say)        # 車隊的隊長要帶頭打首領，血量維持在八成五以上
            if CARAVAN_DISCARD:
                discard_listed(session, keep=unfinished_items(session, task), say=say)
                discard_pets(session, say=say)
            say(f"任務 {task} {tasks[task]['title']}：第 {step}／{total} 步")
            ran.add(leader)
            try:
                progress = run_step(session, map_dir, task, step, say=say,
                                    fight=lambda current, say=say: say(
                                        f"全隊打了 {fight_present(current, say=say, allow_escape=False)} 回合"))
            except ProtocolError as error:
                # 被召喚時落在牆裡（達那村長老房間 5106，五號落在 (25,11)，四周都是牆）：
                # 回記錄點、在那裡把全隊召喚過來，下一圈再一起傳送出發；同一隻只救一次。
                jumped = (leader, step, session.world.map_id)
                if ("預期在地圖" in str(error) and session.sock is not None and jumped not in relogged
                        and session.world.map_id in route_maps):
                    # 換圖點把人直接送到腳本後面的地圖（碧青洞窟 21215 → 21308，腳本寫的是一層層走 21315～21309）：
                    # 這張圖還在路線上，就從那裡接著走。同一張圖只接一次。
                    relogged.add(jumped)
                    say(f"{error}——這張圖在路線後段，從這裡接著走")
                    continue
                if "摸黑也走不到" in str(error) and (leader, step) not in relogged and session.sock is not None:
                    # 打贏通道上的守衛後，這條連線認定的位置常和伺服器不一致，移動全被忽略；
                    # 原地重新登入拿到正確位置就走得動（風的守護獸，2026-11-19／20 實測兩次）。同一步只做一次。
                    relogged.add((leader, step))
                    say(f"{error}——原地重新登入取得正確位置，再接著走")
                    session.close()
                    time.sleep(3.0)
                    session = connect(leader)
                    formed = False
                    continue
                if (("通行路線" not in str(error) and "走不到" not in str(error))
                        or session.sock is None or leader in rescued
                        or f"{task}-{step}" in load_rallies()):
                    # 集合點的步驟不回記錄點重來：那會把全隊帶離首領，而首領前面常有只收單人的關卡
                    # （2026-11-19 深紅洞窟：二號過不了奇寶就把全隊傳走了）。
                    raise
                held = [name for name in load_no_return() if session.world.has_item(name)]
                if held:
                    raise ProtocolError(f"{error}（帶著{'、'.join(held)}不能回記錄點重來，已停止）") from error
                rescued.add(leader)
                say(f"找不到路（{session.world.x},{session.world.y}），先回記錄點，再帶全隊重新傳送過來")
                if session.battle.active:
                    fight_present(session, say=say, allow_escape=False)
                session.return_to_record()
                reform(session, leader, members, say=say, summon=True)
                continue
            known[leader] = progress.next_step(task)
        raise ProtocolError("車隊回合數超過上限，已停止")
    finally:
        session.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--map-dir", type=Path)
    parser.add_argument("--sub", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--task", type=int, help="要做的任務編號；不給就只列出進度")
    parser.add_argument("--party", action="store_true",
                        help="用隊長帶五人隊伍去做（途中遇敵要全隊才能打）；需要其他四隻不是剛單獨登入過")
    parser.add_argument("--team", help="自己組一隊去做：逗號分隔的角色編號，第一隻是隊長（例如 3,4,5）")
    parser.add_argument("--caravan", help="車隊模式：逗號分隔的角色編號（第一隻是目前的隊長），全隊一起走、輪流當隊長做各自的下一步")
    parser.add_argument("--controller", type=int, help="配合 --caravan：目前握有五控操作權的角色（上次沒正常交接時要指定）")
    parser.add_argument("--stay", action="store_true", help="配合 --team：不回記錄點，在目前位置原地組隊（集合打首領用）")
    parser.add_argument("--room", type=int, default=3, choices=range(0, 16), help="開始前至少要有幾格背包空位，不夠就丟肉（預設 3）")
    parser.add_argument("--keep-pets", action="store_true", help="不把多餘寵物存共享寵物倉庫（倉庫滿了的時候用）")
    parser.add_argument("--port", type=int, help="改用別的線路埠（要和 --server-index 一起給；例如一線 9001／4）")
    parser.add_argument("--server-index", type=int, help="登入線路編號")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if config.get("test_only") is not True:
            raise ValueError("只接受測試帳號設定")
        if args.keep_pets:
            global STORE_SPARE_PETS
            STORE_SPARE_PETS = False
        if (args.port is None) != (args.server_index is None):
            raise ValueError("--port 和 --server-index 要一起給")
        if args.port is not None:
            config["port"], config["server_index"] = args.port, args.server_index
        tasks = load_tasks()
        if args.party:
            return run_with_party(config, args.map_dir, args.task)
        if args.caravan:
            return run_caravan(config, args.map_dir, args.task, tuple(int(part) for part in args.caravan.split(",")),
                               controller=args.controller)
        if args.team:
            return run_with_team(config, args.map_dir, args.task, tuple(int(part) for part in args.team.split(",")),
                                 stay=args.stay)
        with Session(timeout=20) as session:
            session.authenticate(config["host"], int(config["port"]), config["account"], str(args.sub),
                                 config["subaccounts"][str(args.sub)]["password_md5"],
                                 server_index=int(config["server_index"]))
            session.request_characters()
            session.select_character()
            session.read_initial_world()
            session.sync()
            end = time.monotonic() + 3      # 登入當下若還卡在戰鬥裡，通知會晚一點才到
            while time.monotonic() < end:
                session.poll_event(0.3)
            if session.battle.active:
                print("登入時還在戰鬥中，先脫離")
                fight(session)
            progress = progress_of(session)
            if args.task is None:
                for task in tasks.values():
                    state = "已完成" if task["id"] in progress.done else (
                        f"進行中（做完第 {progress.steps[task['id']]} 步）" if task["id"] in progress.steps else "未接")
                    print(f"{task['id']:>4} {task['title']}：{state}")
                return 0
            if args.task not in tasks or args.map_dir is None:
                raise ValueError("任務編號不在任務表內，或沒有給 --map-dir")
            if solo_hp_percent(session.world) < SOLO_HEAL_BELOW:
                heal_with_items(session)
            discard_listed(session, keep=unfinished_items(session, args.task))
            if session.world.items is not None and session.world.free_bag_slots() < max(args.room, 4):
                from .storage import store_items
                store_items(session, task_items(args.task))      # 先把別的任務留下的道具收進個人道具倉庫
            make_room(session, args.room)
            total = len(tasks[args.task]["steps"])
            for _ in range(total):
                step = progress.next_step(args.task)
                if step is None:
                    break
                print(f"任務 {args.task} {tasks[args.task]['title']}：第 {step}／{total} 步")
                progress = run_step(session, args.map_dir, args.task, step)
            print(f"任務 {args.task}：" + ("已完成" if args.task in progress.done else "尚未完成"))
            return 0
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        print(f"任務流程停止：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
