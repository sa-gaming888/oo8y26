"""練功總控：自己循環「整備 → 組隊 → 選地圖 → 傳送 → 練功 → 補血」，直到全隊達到目標等級。

架構參考 primalSA 的 `primalsa/autoplay/supervisor.py`：決策（decide）是純函式、可測；執行（Supervisor）才碰遊戲。

    python -X utf8 -u -m oo8y26.supervisor --config config/query.local.json \\
        --map-dir <客戶端 map 目錄> --target-level 131

規則：
- 全隊最低等級 ≥ 目標 → 結束。
- 隊伍最低血量（人物、騎寵、戰寵）< 50% → 優先補血；連續補 3 次仍過低就停。
- 依全隊最低等級選路線上「門檻已達」的最後一張圖；不在那張圖就回記錄點再貼心傳送。
- 某張圖在時間窗內補血太頻繁或全滅 → 退回上一張一段時間（打不過）。
- 每升若干級做一次整備：五隻各自單獨登入配點、智慧果到期就再吃一顆；之後等子號離線再組隊。
- 出現人工驗證、未知錯誤、連續失敗超過上限、超過總時間 → 停止並寫明原因，不無限重試。

補血方式：回記錄點 → 走出薩村 → 在村口打一場（該地圖戰後全隊補滿，2026-11-17 實測）。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import allocate as allocate_module
from .session import ProtocolError, Session

# 練功路線：(名稱, 貼心傳送代號, 換過來的最低等級)。代號來自傳送清單（2026-11-17 實讀）。
# 前四張用伺服器的傳送門檻；後兩張比門檻（80／110）高：2026-11-17 實測 81 級進漆黑的洞穴遇到 95 級的怪，
# 打不贏還有戰寵陣亡，所以等等級接近再跳（打不過仍由退圖機制處理）。
ROUTE = (
    ("阿布的洞穴[10-25]", 101, 1),
    ("海底的通道[25-45]", 102, 25),
    ("五兄弟山寨[45-60]", 103, 45),
    ("地下的一楼[60-85]", 104, 60),
    ("漆黑的洞穴[80-110]", 105, 92),
    ("英雄岛[115-135]", 106, 111),   # 漆黑洞穴後段幾乎沒經驗：舊隊伍 113 級近兩小時升不了一級；02 隊 2026-11-20 在 112 級打了 500 場才升一級，再提早
)
GATE_MAP = 100                 # 薩村外：戰後全隊補滿
GATE_POSITIONS = [[637, 493], [637, 494]]
MEAT_MIN_FREE, MEAT_FREE_TO = 3, 6   # 背包空格少於幾格就丟肉、清到幾格（同「自動丟肉」模組的預設）
EAT_MEAT_BELOW = 80             # 一號或牠的寵物血量低於這個百分比就吃肉（同「自動吃肉」模組的預設）
LOYALTY_RETRY_BATTLES = 20     # 練忠誠時每幾場重試一次優先騎寵
GATE_UNTIL = 10                # 全隊未滿這個等級先在村口練
GATE_ENEMY_LEVEL = 15          # 村口補血那一場：怪超過這個等級就不打
RECORD_MAP = 1006              # 記錄點（村長家）
HEAL_BELOW = 50                # 血量低於這個百分比就補血
MAX_CONSECUTIVE_HEALS = 3
TROUBLE_WINDOW = 900.0         # 秒：這段時間內同一張圖出事（補血、全滅）幾次算打不過
TROUBLE_LIMIT = 2
RETREAT_SECONDS = 1800.0       # 退圖後多久才再嘗試前進
MAINTAIN_EVERY_LEVELS = 5      # 每升幾級整備（配點）一次
BAG_EVERY_LEVELS = 5           # 每幾級順便清一次隊員背包（要多登入一次，不必每級做）
BATTLES_PER_ROUND = 40
ROUND_SECONDS = 900
MAX_FAILURES = 5               # 連續失敗幾次就停
PARTY_WAIT_SECONDS = 120.0     # 組隊失敗（子號狀態未清、伺服器冷卻）後等多久
PARTY_WAIT_LIMIT = 10
PARTY_RESET_AT = 3             # 組隊連續失敗到第幾次時，讓子號各自登入一次清狀態
ROSTER_MISMATCH_TEXT = "戰鬥角色與指定五人隊伍不符"   # 隊長卡在一場隊伍不完整的戰鬥裡：一樣要先清狀態
STUCK_IN_BATTLE_TEXT = "战斗中"  # 「子账号战斗中，无法召唤」：等待無效，第一次看到就清狀態
FRUIT_SECONDS = 8 * 3600       # 一顆智慧果的效期
BEST_FRUIT = "五倍智慧果(1小时)"   # 使用者 2026-11-24：身上有五倍果就優先吃
BEST_FRUIT_SECONDS = 3600
MALL_FRUIT = "双倍智慧果(4小时)"   # 背包沒有果子時到商城石幣分頁買的那一種
MALL_FRUIT_PRICE = 20000
MALL_FRUIT_SECONDS = 4 * 3600
FRUIT_MARGIN = 120.0
MAINTAIN_EVERY_SECONDS = 3600.0   # 沒有別的理由也至少這麼久整備一次           # 智慧果到期後多久內要回去整備（秒）


@dataclass
class State:
    """決策需要的全部資訊；由執行端每一圈更新。"""
    minimum_level: int | None = None
    hp_percent: int | None = None
    map_id: int | None = None
    zone_maps: dict = field(default_factory=dict)        # 路線索引 → 實際到達後的地圖編號
    consecutive_heals: int = 0
    trouble: dict = field(default_factory=dict)          # 路線索引 → [出事時間…]
    retreat_until: float = 0.0
    retreat_cap: int | None = None                       # 退圖期間最高只到這個路線索引
    last_maintained_level: int | None = None
    last_maintained_at: float = float("-inf")      # 上次整備的時間（總控的時鐘）
    failures: int = 0
    verification: bool = False
    fruit_due: float | None = None                       # 智慧果最早到期的時間（總控時鐘）；None＝不知道
    flee: bool = False                                   # 上一圈在練功圖出事：下一圈若還在戰鬥中就逃跑


def allowed_zone(level: int) -> int:
    """全隊最低等級可以去的最後一張圖（路線索引）。"""
    index = 0
    for i, (_, _, need) in enumerate(ROUTE):
        if level >= need:
            index = i
    return index


def maintain_due(state: State, now: float) -> bool:
    """該不該回去配點：全隊最低等級比上次配點時多了 MAINTAIN_EVERY_LEVELS 級（使用者 2026-11-20：統一 5 級配一次；
    每次重新登入啟動時另外會先檢查、配一次，見 Supervisor.run 開頭）。"""
    return state.minimum_level - (state.last_maintained_level or 0) >= MAINTAIN_EVERY_LEVELS


def decide(state: State, *, target_level: int, now: float) -> tuple:
    """回傳 (動作, 參數)。動作：stop / done / maintain / heal / travel / train。"""
    if state.verification:
        return "stop", "出現人工驗證，交給使用者處理"
    if state.failures >= MAX_FAILURES:
        return "stop", f"連續失敗 {state.failures} 次"
    if state.minimum_level is None:
        return "stop", "讀不到隊伍等級"
    if state.minimum_level >= target_level:
        return "done", f"全隊最低等級 {state.minimum_level}"
    if state.last_maintained_level is None or maintain_due(state, now):
        return "maintain", None
    if state.fruit_due is not None and now >= state.fruit_due + FRUIT_MARGIN:
        return "maintain", "智慧果到期"
    if state.hp_percent is not None and state.hp_percent < HEAL_BELOW:
        if state.consecutive_heals >= MAX_CONSECUTIVE_HEALS:
            return "stop", f"連續補血 {state.consecutive_heals} 次，血量仍只有 {state.hp_percent}%"
        return "heal", f"隊伍最低血量 {state.hp_percent}%"
    index = allowed_zone(state.minimum_level)
    if now < state.retreat_until and state.retreat_cap is not None:
        index = min(index, state.retreat_cap)
    if state.zone_maps.get(index) is None or state.map_id != state.zone_maps[index]:
        return "travel", index
    return "train", index


def note_trouble(state: State, index: int, now: float) -> bool:
    """記錄某張圖出事一次；時間窗內達到上限就啟動退圖，回傳是否退圖。"""
    recent = [t for t in state.trouble.get(index, []) if now - t <= TROUBLE_WINDOW] + [now]
    state.trouble[index] = recent
    if len(recent) >= TROUBLE_LIMIT and index > 0:
        state.retreat_until = now + RETREAT_SECONDS
        state.retreat_cap = index - 1
        state.trouble[index] = []
        return True
    return False


def team_hp_percent(world) -> int:
    """全隊人物、騎寵、戰寵的最低血量百分比；資料不完整就 raise。"""
    lowest = 100
    for slot in range(1, 6):
        actor = world.actor(slot)
        units = [(actor.hp, actor.max_hp)]
        for index in (actor.mount_pet_index, actor.battle_pet_index):
            if index == -1 and index == actor.mount_pet_index != actor.battle_pet_index:
                continue          # 沒有騎寵（轉生後等級太低，暫時沒有騎得上的）：只看人物和戰寵
            if index is None or index not in actor.pets:
                raise ProtocolError("隊伍寵物狀態不完整，無法判斷血量")
            units.append((actor.pets[index].hp, actor.pets[index].max_hp))
        for hp, maximum in units:
            if hp is None or not maximum:
                raise ProtocolError("隊伍血量資料不完整")
            lowest = min(lowest, hp * 100 // maximum)
    return lowest


def team_snapshot(world) -> list:
    """五隻角色與其騎寵、戰寵的現況（寫進狀態檔，方便不登入就能查）。"""
    def pet(actor, index):
        found = actor.pets.get(index) if index is not None else None
        if found is None:
            return None
        return {"name": found.name.decode("gbk", "replace"), "level": found.level,
                "hp": found.hp, "max_hp": found.max_hp}

    team = []
    for slot in range(1, 6):
        actor = world.actor(slot)
        team.append({"slot": slot, "level": actor.level, "hp": actor.hp, "max_hp": actor.max_hp,
                     "mp": actor.mp, "max_mp": actor.max_mp,
                     "mount": pet(actor, actor.mount_pet_index), "fighter": pet(actor, actor.battle_pet_index),
                     "pets": len(actor.pets)})
    return team


def team_report(world) -> list:
    """給日誌用的隊伍狀態：第一行是地圖，之後每隻角色一行（名稱、等級、生命、騎寵與戰寵的等級與生命）。"""
    def pet(found):
        return "無" if found is None else f"{found['name']} Lv{found['level']} {found['hp']}/{found['max_hp']}"

    lines = [f"隊伍狀態：地圖 {world.map_id} 座標 ({world.x},{world.y})"]
    for row in team_snapshot(world):
        role = world.roles.roles.get(row["slot"])
        name = role.name.decode("gbk", "replace") if role is not None and isinstance(role.name, bytes) else "?"
        lines.append(f"  {row['slot']} 號 {name}：Lv{row['level']} 生命 {row['hp']}/{row['max_hp']}"
                     f"｜騎 {pet(row['mount'])}｜戰 {pet(row['fighter'])}")
    return lines


CROWDED_TEXT = "已经有超过"       # 「您的当前坐标已经有超过3名玩家，无法建立新的原地遇敌」
RELOCATE_RINGS = (6, 9, 12, 15, 18)


def relocation_candidates(grid, origin, attempt: int, salt: int = 0) -> list:
    """擁擠時要改站的候選格：以 origin 為中心、半徑隨次數加大的八個方向，只留可通行的。
    salt 是每個帳號固定的數字：不同隊伍先試的方向、半徑都錯開。2026-11-24 七隊同時在英雄島，
    每隊都照同一個順序挪到同一格，挪了還是擠，fusion05 連續失敗 5 次停下。"""
    radius = RELOCATE_RINGS[min(attempt, len(RELOCATE_RINGS) - 1)] + salt // 8 % 3
    x, y = origin
    points = [(x + dx * radius, y + dy * radius)
              for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))]
    shift = (attempt + salt) % len(points)
    points = points[shift:] + points[:shift]
    return [point for point in points if grid.walkable(point)]


def write_plan(path: Path, map_id: int, *, positions, maximum_enemy_level: int) -> None:
    """產生給既有戰鬥流程用的單一階段計畫檔（血量由總控自己把關，計畫內門檻設最低）。"""
    # 以目前指定的計畫檔（環境變數 oo8y26_TRAINING_PLAN）為底：不同隊伍鎖定的騎寵、戰寵不同，
    # 一律拿預設那份當底會把別隊的寵物設定蓋掉（2026-11-23，02 隊集合打龍王守衛時「指定寵物不存在」）。
    override = os.environ.get("oo8y26_TRAINING_PLAN")
    source = Path(override) if override and Path(override).is_file() else (
        Path(__file__).resolve().parents[1] / "data" / "training_plan.json")
    base = json.loads(source.read_text(encoding="utf-8"))
    base["stages"] = [{
        "id": f"map_{map_id}", "minimum_team_level": 1, "location": f"地圖 {map_id}", "map_id": map_id,
        "positions": positions, "allowed_monsters": ["*"], "maximum_enemy_level": maximum_enemy_level,
        "maximum_rounds": 30, "maximum_battle_seconds": 290,
    }]
    base["limits"]["minimum_hp_percent"] = 0       # 戰鬥中有單位陣亡也不中途丟下戰鬥；戰前血量由總控檢查
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")


class Supervisor:
    def __init__(self, config: dict, *, map_dir: Path, target_level: int, maximum_seconds: float,
                 ratio=(10, 9, 0, 5), log=print, clock=time.monotonic, sleep=time.sleep):
        self.config, self.map_dir, self.target_level = config, map_dir, target_level
        self.deadline = clock() + maximum_seconds
        self.ratio, self.log, self.clock, self.sleep = ratio, log, clock, sleep
        self.state = State()
        self.plan_path = Path("captures") / "supervisor-plan.local.json"
        self.status_path = Path("captures") / "supervisor-status.local.json"
        self.quest_path = Path("captures") / "quest-request.local.json"
        self.battles = 0
        self.ledger = {}             # 角色編號 → (名稱, 石幣, 聲望)：整備登入時讀到的，寫進日誌給定時回報用
        self.crowded = 0             # >0 表示上一圈因落點擁擠失敗，下一圈先改站（值＝第幾次）
        self.crowded_total = 0
        self.reset_done = False      # 這一次組隊失敗期間是否已讓子號清過狀態
        self.premaintained = False   # 啟動時是否已先整備過
        self.raising = []            # 優先騎寵忠誠不足、還在村口練的角色
        self.gate_until = GATE_UNTIL # 全隊未滿這個等級先在村口練；None＝還沒決定（轉生過的隊伍會設成 0，直接飛第一張練功圖）
        self.gate_forced = False     # 容器用環境變數指定了 gate_until：不再依轉生數自動調整
        self.before_battle = None    # 每場開打前呼叫（容器用它在兩場之間處理停止訊號）
        self.status_seconds = 60.0   # 每隔幾秒印一次隊伍狀態；0＝不印
        self.last_report = float("-inf")
        self.fast = True             # 練功的每一場都用快速戰鬥；False＝原本逐回合確認的流程

    # ---- 小工具
    def say(self, message: str) -> None:
        self.log(f"{time.strftime('%H:%M:%S')} [總控] {message}")

    def connect(self) -> Session:
        session = Session(timeout=10)
        session.authenticate(self.config["host"], int(self.config["port"]), self.config["account"], "1",
                             self.config["subaccounts"]["1"]["password_md5"],
                             server_index=int(self.config["server_index"]))
        session.request_characters()
        session.select_character()
        session.read_initial_world()
        return session

    def use_plan(self, map_id: int, *, positions=None, maximum_enemy_level=250) -> None:
        write_plan(self.plan_path, map_id, positions=positions, maximum_enemy_level=maximum_enemy_level)
        os.environ["oo8y26_TRAINING_PLAN"] = str(self.plan_path)

    def party(self, session: Session) -> None:
        """組隊並設好騎寵、戰寵，更新等級、血量、地圖。"""
        # 子號被單獨登入過之後，名冊會殘留「在線／跟隨」狀態約 6~8 分鐘才變離線（2026-11-18 逐分鐘實測），
        # 殘留期間伺服器不會推送它的寵物資料。這時去召喚／組隊雖然會成功，卻拿不到資料，
        # 還會讓它一直維持在線、等不到重新登入（實測卡了 16 分鐘）。所以殘留期間完全不碰，只等待：
        # 判斷方式＝不是離線、又沒有寵物資料。
        roles = session.request_roles()
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            session.poll_event(0.25)
        if any(role.slot != 1 and role.status != 0 and not session.world.actor(role.slot).pets for role in roles):
            # 不用等它自己變離線：由隊長把隊員下線（O|編號）再一鍵登入、召喚、組隊，十幾秒就好
            # （quest.reform，2026-11-19 實測；2026-11-20 使用者指出這段等待是流程造成的，組隊不需要等離線）。
            from . import quest
            self.say("隊員是單獨登入後的殘留狀態：由隊長下線再重新叫上線")
            quest.reform(session, 1, (2, 3, 4, 5), say=self.say, summon=True)
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                session.poll_event(0.25)
        session.ensure_party()
        if any(not session.world.actor(slot).pets for slot in range(2, 6)):
            raise ProtocolError("隊員寵物資料未推送")
        self.use_plan(session.world.map_id or 0)       # ensure_pets 會讀計畫裡鎖定的寵物名稱
        session.ensure_pets()
        self.note_loyalty(session)
        if not self.gate_forced:
            from . import quest
            rebirth = quest.rebirth_of(session.world)
            # 轉生過的隊伍帶著高等的戰寵，1 級就能直接飛阿布洞穴（龜洞）用快戰練，不必先在村口練到 10 級
            # （使用者 2026-11-21）。
            wanted = 0 if rebirth else GATE_UNTIL
            if wanted != self.gate_until:
                self.say(f"隊長轉生數 {rebirth}：{'直接飛第一張練功圖，不在村口練' if wanted == 0 else f'未滿 {wanted} 級先在村口練'}")
            self.gate_until = wanted
        self.refresh(session)

    def note_loyalty(self, session: Session) -> None:
        """有角色的優先騎寵還不能騎（忠誠不足）＝要先留在村口練忠誠；狀態有變就記一行。"""
        from .training import raise_loyalty
        if getattr(session, "_pets_verified", True) is not True:
            return      # 連上時還在戰鬥中、沒有真的檢查寵物：維持原本的判斷（2026-11-20 這種時候誤判成「都騎上了」）
        waiting = (sorted(set(getattr(session, "_ride_refused", {})) - set(getattr(session, "_unmounted", ())))
                   if raise_loyalty() else [])
        riderless = sorted(getattr(session, "_unmounted", ()))
        if riderless and riderless != getattr(self, "riderless", None):
            self.say(f"暫時沒有騎得上的寵物、先不騎的角色：{riderless}（騎寵等級最多只能比人物高 5 級；之後會定時再試）")
        self.riderless = riderless
        if waiting != self.raising:
            self.say(f"騎寵忠誠不足、先不騎讓牠出戰練起來的角色：{waiting}" if waiting
                     else "全隊的優先騎寵都騎上了，回正常的練功路線")
        self.raising = waiting

    def refresh(self, session: Session) -> None:
        self.state.minimum_level = session.world.roles.minimum_level()
        self.state.hp_percent = team_hp_percent(session.world)
        self.state.map_id = session.world.map_id
        self.team = team_snapshot(session.world)

    def save_status(self, action: str, detail) -> None:
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            self.status_path.write_text(json.dumps({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "detail": detail,
                "minimum_level": self.state.minimum_level, "hp_percent": self.state.hp_percent,
                "map_id": self.state.map_id, "battles": self.battles, "target_level": self.target_level,
                "team": getattr(self, "team", None),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- 各個動作
    def maintain(self) -> None:
        """五隻各自單獨登入：配完剩餘點數、智慧果到期就再吃一顆。"""
        from . import newbie
        lefts = []
        for slot in range(1, 6):
            result = allocate_module.allocate(self.config, slot, self.ratio)
            self.say(f"子號 {slot} 配點：原有 {result['points']} 點，生命上限 {result['hp'][1]}")
            self.sleep(2.0)
            if (self.state.minimum_level or 0) % BAG_EVERY_LEVELS == 0:
                self.clear_bag(slot)
            self.claim_daily(slot)
            self.swap_pets(slot)
            left = self.fruit_left(slot)
            if left is not None and left > BEST_FRUIT_SECONDS:
                # 剩餘時間超過一小時＝現在是雙倍果的加成。五倍和雙倍只能擇一（後吃的蓋掉前面的），
                # 智慧果不用省（2026-11-24 高手提醒、使用者轉述）：身上有五倍果就直接吃掉蓋過去。
                session, outfitter = newbie._connect(self.config, slot)
                with session:
                    outfitter.load()
                    if self.eat_best(outfitter, slot):
                        self.say(f"子號 {slot}：身上有五倍智慧果，直接吃（蓋掉剩 {left // 60} 分鐘的雙倍）")
                        left = BEST_FRUIT_SECONDS
                self.sleep(2.0)
            if left == 0:
                session, outfitter = newbie._connect(self.config, slot)
                with session:
                    outfitter.load()
                    if self.eat_best(outfitter, slot):
                        self.say(f"子號 {slot}：智慧果已到期，吃一顆五倍的（1 小時）")
                        left = BEST_FRUIT_SECONDS
                    elif outfitter.use_expecting(newbie.WISDOM_FRUIT, "学习经验的能力提升"):
                        self.say(f"子號 {slot}：智慧果已到期，再吃一顆")
                        left = FRUIT_SECONDS
                    else:
                        left = self.buy_fruit(session, outfitter, slot)
                self.sleep(2.0)
            lefts.append(left)
            name, gold, fame = self.ledger.get(slot, (None, None, None))
            # 給定時回報用的一行（石幣、聲望是這次整備登入時讀到的；加成是剩餘分鐘）
            self.say(f"子號 {slot} 帳務：{name or '?'}｜石幣 {gold if gold is not None else '?'}｜聲望 {fame if fame is not None else '?'}"
                     f"｜經驗加成剩 {left // 60 if left else 0} 分鐘")
        known = [left for left in lefts if left]
        # 五隻都讀得到才排下一次按時間整備；任何一隻讀不到或沒果子就不排，避免空轉。
        # 至少每 MAINTAIN_EVERY_SECONDS 回來整備一次：領當天陸續到門檻的在線獎勵、更新石幣聲望的紀錄、檢查加成有沒有斷
        self.state.fruit_due = self.clock() + min(min(known) if len(known) == 5 else MAINTAIN_EVERY_SECONDS, MAINTAIN_EVERY_SECONDS)
        if known:
            self.say(f"智慧果最早 {min(known) // 60} 分鐘後到期")

    def eat_best(self, outfitter, slot: int) -> bool:
        """吃一顆五倍智慧果；身上沒有回 False。吃不下去（例如等級不到道具要求）只記一行、回 False，
        讓後面照常吃雙倍的——不能因為一顆果子讓整備失敗。連線斷了才往上丟。"""
        try:
            return outfitter.use_confirming(BEST_FRUIT, "学习经验的能力提升")
        except ProtocolError as error:
            if outfitter.session.sock is None:
                raise
            self.say(f"子號 {slot}：五倍智慧果沒有吃成（{error}）")
            return False

    def swap_pets(self, slot: int) -> None:
        """整備時檢查騎寵、戰寵：練滿 140 級就換一隻沒滿的（見 petswap.py）。換不成只記一行，不影響練功。"""
        from . import newbie, panels, petswap, training
        try:
            session, outfitter = newbie._connect(self.config, slot)
            with session:
                outfitter.load()
                mounts, fighters = training.allowed_pets()
                level = (panels.character_panel(session.world) or {}).get("等級") or 1
                petswap.swap_maxed(session, outfitter, slot, say=lambda message: self.say(f"子號 {slot}：{message}"),
                                   mount_names=tuple(dict.fromkeys(training.role_mounts(slot) + mounts)),
                                   fighter_names=fighters, character_level=level)
        except (ProtocolError, OSError, TimeoutError) as error:
            self.say(f"子號 {slot}：換滿級寵物沒有完成（{error}）")
        self.sleep(2.0)

    def claim_daily(self, slot: int) -> None:
        """整備時順便領每日在線獎勵（四顆五倍智慧果，見 daily.py）。領不到只記一行，不重試、不影響練功。"""
        from . import daily, newbie, panels
        try:
            session, _ = newbie._connect(self.config, slot)
            with session:
                # 聲望在貨幣資料（S262）裡，要送 C261 (0) 伺服器才給（官方客戶端進世界後也會送；2026-11-24 實測）
                session._send_command(261, (0,), time.monotonic() + session.timeout)
                end = time.monotonic() + 5.0
                while time.monotonic() < end and session.world.raw.get("貨幣") is None:
                    session.poll_event(0.3)
                panel = panels.character_panel(session.world) or {}
                self.ledger[slot] = (panel.get("名稱"), panel.get("石幣"), panel.get("聲望"))
                daily.claim_online_rewards(session, say=lambda message: self.say(f"子號 {slot}：{message}"))
        except (ProtocolError, OSError, TimeoutError) as error:
            self.say(f"子號 {slot}：在線獎勵沒有領成（{error}）")
        self.sleep(2.0)

    def buy_fruit(self, session, outfitter, slot: int):
        """背包沒有智慧果：到商城石幣分頁買一顆「双倍智慧果(4小时)」（價格超過上限不買）吃掉。
        買不到、吃不下去都只記一行、不重試，回 None（不再為了它回來整備）。"""
        from . import mall
        from .session import ProtocolError
        try:
            if not session.world.has_item(MALL_FRUIT):
                mall.buy_with_stone(session, [MALL_FRUIT], say=lambda message: None, maximum_price=MALL_FRUIT_PRICE)
                outfitter.items = {key: value[0] for key, value in (session.world.items or {}).items() if value and value[0]}
            if outfitter.use_expecting(MALL_FRUIT, "学习经验的能力提升"):
                self.say(f"子號 {slot}：背包沒有智慧果，到商城買一顆 4 小時的（{MALL_FRUIT_PRICE} 石幣）吃了")
                return MALL_FRUIT_SECONDS
        except (ProtocolError, OSError, TimeoutError) as error:
            self.say(f"子號 {slot}：買智慧果沒有成功（{error}）")
            return None
        self.say(f"子號 {slot}：智慧果已到期，買了也沒吃成")
        return None

    def clear_bag(self, slot: int) -> None:
        """隊員的背包只有牠自己登入時清得到：整備時順便把塞滿的肉丟掉（2026-11-20 舊隊伍四隻隊員的背包都被肉塞滿過）。"""
        from . import newbie, quest
        session, outfitter = newbie._connect(self.config, slot)
        with session:
            if session.world.items is not None and session.world.free_bag_slots() < MEAT_MIN_FREE:
                try:
                    quest.make_room(session, MEAT_FREE_TO, say=lambda message: self.say(f"子號 {slot}：{message}"))
                except ProtocolError as error:
                    # 背包裡沒有肉可清（例如被剛領的智慧果佔滿）：不是練功的錯，記一行就好；連線斷了才往上丟
                    if session.sock is None:
                        raise
                    self.say(f"子號 {slot}：背包快滿但沒有肉可清（{error}）")
        self.sleep(2.0)

    def fruit_left(self, slot: int):
        """讀狀態訊息 A 的第 4 段（經驗加成剩餘秒數）；讀不到回 None（不亂吃）。"""
        seconds = {}
        session = Session(timeout=10)
        original = session.world._status

        def watch(message: bytes):
            if message[:2] == b"A|":
                parts = message.split(b"|")
                if len(parts) >= 5 and parts[4].isdigit():
                    seconds["left"] = int(parts[4])
            return original(message)

        session.world._status = watch
        with session:
            session.authenticate(self.config["host"], int(self.config["port"]), self.config["account"], str(slot),
                                 self.config["subaccounts"][str(slot)]["password_md5"],
                                 server_index=int(self.config["server_index"]))
            session.request_characters()
            session.select_character()
            session.read_initial_world()
            session.sync()
            end = time.monotonic() + 1.5
            while time.monotonic() < end and "left" not in seconds:
                session.poll_event(0.25)
        return seconds.get("left")

    def reset_members(self) -> None:
        """五隻角色（含隊長）各自單獨登入進世界一次、逃離殘留的戰鬥再離線；任何一隻失敗只記錄，不中斷。
        2026-11-21 01 在英雄島：一隻隊員掉出隊伍後，隊長自己卡在一場名冊不符的戰鬥裡（「战斗中，无法召唤队友」），
        只清四隻子號解不開，重試 10 次後停了四個小時。"""
        from . import newbie
        for slot in range(1, 6):
            self.sleep(3.0)
            try:
                with Session(timeout=10) as session:
                    session.authenticate(self.config["host"], int(self.config["port"]), self.config["account"], str(slot),
                                         self.config["subaccounts"][str(slot)]["password_md5"],
                                         server_index=int(self.config["server_index"]))
                    session.request_characters()
                    session.select_character()
                    session.read_initial_world()
                    newbie.finish_leftover_battle(session)      # 要先要一次名冊，伺服器才會補送進行中的戰鬥
                self.say(f"子號 {slot} 已單獨登入一次清狀態")
            except (ProtocolError, OSError) as error:
                self.say(f"子號 {slot} 單獨登入未成功（{error}）")

    def heal(self, session: Session) -> None:
        """回記錄點 → 出村 → 村口打一場（戰後全隊補滿）。"""
        self.finish_battle(session)
        from . import quest
        if session.world.map_id != GATE_MAP:
            quest.go_to_gate(session, self.map_dir, say=self.say)
        self.use_plan(GATE_MAP, positions=None, maximum_enemy_level=GATE_ENEMY_LEVEL)
        session.test_novice_battle(maximum_rounds=8, maximum_seconds=120)
        session.request_roles()
        self.refresh(session)

    def finish_battle(self, session: Session) -> None:
        """移動前把殘留戰鬥打完（例如村口補血後又遇敵）；戰鬥中不能回記錄點或傳送。"""
        if session.battle.active:
            self.use_plan(session.world.map_id, maximum_enemy_level=250)
            session.test_novice_battle(maximum_rounds=30, maximum_seconds=290)
            session.request_roles()

    def travel(self, session: Session, index: int) -> None:
        name, code, _ = ROUTE[index]
        self.finish_battle(session)
        if index == 0 and (self.state.minimum_level or 1) < self.gate_until:
            # 未滿 GATE_UNTIL 級先在村口練（從記錄點走出村，不用傳送）：
            # 2026-11-20 實測一級隊伍直接進阿布洞穴第一場就全滅。到了等級由主迴圈清掉這筆紀錄，改傳送去阿布洞穴。
            from . import quest
            quest.go_to_gate(session, self.map_dir, say=self.say)
            self.state.zone_maps[index] = self.state.map_id = session.world.map_id
            self.say(f"未滿 {self.gate_until} 級先在村口練：地圖 {session.world.map_id} 座標 ({session.world.x},{session.world.y})")
            return
        if session.world.map_id != RECORD_MAP:
            session.return_to_record()
        map_id, x, y = session.teleport(code)
        self.state.zone_maps[index] = map_id
        self.state.map_id = map_id
        self.say(f"已傳送到 {name}：地圖 {map_id} 座標 ({x},{y})")

    def quest_request(self):
        """讀取並取走任務請求檔（內容 {"task": 編號}）；沒有或格式不對回 None。先刪檔再做，失敗也不會重複執行。"""
        try:
            task = json.loads(self.quest_path.read_text(encoding="utf-8-sig")).get("task")
            self.quest_path.unlink()
        except (OSError, ValueError, AttributeError):
            return None
        return task if isinstance(task, int) else None

    def run_quest(self, session: Session, task: int) -> None:
        """在兩輪練功之間，用目前已組好隊的連線做一個任務；做完由下一圈自己傳回練功圖。"""
        import importlib
        from . import quest
        quest = importlib.reload(quest)      # 任務流程還在探索期：每次請求都重新載入，改流程不必重啟練功
        tasks = quest.load_tasks()
        if task not in tasks:
            self.say(f"任務請求 {task} 不在任務表內，略過")
            return
        self.say(f"收到任務請求：{task} {tasks[task]['title']}")
        try:
            self.finish_battle(session)
            progress = quest.progress_of(session)
            quest.make_room(session, 3, say=self.say)
            for _ in range(len(tasks[task]["steps"])):
                step = progress.next_step(task)
                if step is None:
                    break
                self.say(f"任務 {task} 第 {step}／{len(tasks[task]['steps'])} 步")
                progress = quest.run_step(session, self.map_dir, task, step, say=self.say, fight=self.finish_battle)
            self.say(f"任務 {task}：" + ("已完成" if task in progress.done else "尚未完成"))
        except (ProtocolError, OSError) as error:
            self.say(f"任務 {task} 停止：{error}")
            raise
        finally:
            self.state.map_id = session.world.map_id

    def relocate(self, session: Session) -> None:
        """落點太多人不能原地遇敵：帶隊走到附近的空格。次數有上限，超過就照一般失敗處理。
        地圖資料直接向伺服器要（只要附近那一塊），不依賴本機地圖檔：2026-11-20 容器裡沒有英雄島的地圖檔，
        換站位連續失敗 5 次，02 隊停了五個多小時。"""
        from . import quest
        attempt = self.crowded - 1
        origin = (session.world.x, session.world.y)
        reach = RELOCATE_RINGS[-1] + 6
        grid = quest.fetch_server_grid(session, say=lambda message: None,
                                       area=(origin[0] - reach, origin[1] - reach, origin[0] + reach, origin[1] + reach))
        import zlib
        salt = zlib.crc32(str(self.config.get("account", "")).encode("utf-8"))
        candidates = relocation_candidates(grid, origin, attempt, salt)
        if not candidates:
            raise ProtocolError("附近找不到可以改站的空格")
        quest.walk(session, self.map_dir, candidates[0], say=lambda message: None)
        self.finish_battle(session)
        # 走路時伺服器不回推座標（world.x/y 要到戰鬥結束才更新），所以這裡記的是目標格。
        self.say(f"原位置 {origin} 太多人，改站到 {candidates[0]}（第 {self.crowded} 次）")
        self.crowded = 0

    def battle_once(self, session: Session) -> None:
        """打一場。預設用快速戰鬥（使用者 2026-11-20：只要是會遇敵的地方都要開）：這條連線第一次打之前先開快戰開關，
        之後每場原地遇敵、用快戰打法（不送回合同步，每場約 5 秒 → 2 秒）。fast=False 時用原本逐回合確認的流程。"""
        from . import modules, quest
        if self.before_battle is not None:
            self.before_battle()         # 容器的停止檢查：兩場戰鬥之間才登出
        # 練功預設全開（使用者 2026-11-20）：戰前背包快滿就丟肉、戰後一號或寵物血量低就吃肉。
        if session.world.items is not None and session.world.free_bag_slots() < MEAT_MIN_FREE:
            quest.make_room(session, MEAT_FREE_TO, say=lambda message: None)
        if not self.fast:
            session.test_novice_battle(maximum_rounds=30, maximum_seconds=290)
            modules.eat_meat(session, EAT_MEAT_BELOW, say=self.say)
            return
        try:
            if getattr(session, "_fast_battle_on", False) is not True:
                session._send_command(2025, (f"KS|{session.sock.fileno()}|1|".encode(),), time.monotonic() + 20)
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    session.poll_event(0.25)
                session._fast_battle_on = True
                self.say("已開快速戰鬥")
            session._send_command(202, (17,), time.monotonic() + 20)          # 原地遇敵
            end = time.monotonic() + 10
            while not session.battle.active and time.monotonic() < end:
                session.poll_event(0.2)
            if not session.battle.active:
                raise ProtocolError("送出遇敵後沒有進入戰鬥")
            session._send_command(202, (18,), time.monotonic() + 20)          # 取消後續遇敵
        except BaseException:
            session.close()
            raise
        modules.fast_fight(session)
        modules.eat_meat(session, EAT_MEAT_BELOW, say=self.say)

    def report_team(self, session: Session) -> None:
        """每隔 status_seconds 把人物與隊伍狀態印到日誌（使用者 2026-11-20）；0＝不印。只讀現有資料，不多送封包。"""
        if not self.status_seconds or self.clock() - self.last_report < self.status_seconds:
            return
        self.last_report = self.clock()
        try:
            for line in team_report(session.world):
                self.say(line)
        except (ProtocolError, AttributeError, KeyError) as error:      # 資料不完整時不讓練功跟著停
            self.say(f"隊伍狀態讀不完整：{error}")

    def train(self, session: Session, index: int) -> str:
        """在目前地圖連續打，回傳停下來的原因。每場前由總控檢查血量與等級。"""
        if self.crowded:
            self.relocate(session)
        self.use_plan(session.world.map_id, maximum_enemy_level=250)
        started = self.clock()
        for _ in range(BATTLES_PER_ROUND):
            if self.clock() >= self.deadline:
                return "已達總時間上限"
            if self.clock() - started > ROUND_SECONDS:
                return "本輪時間到"
            if self.state.hp_percent < HEAL_BELOW:
                return "血量過低"
            if self.state.minimum_level >= self.target_level:
                return "達到目標等級"
            if allowed_zone(self.state.minimum_level) > index and self.clock() >= self.state.retreat_until:
                return "可以前進到下一張圖"
            if maintain_due(self.state, self.clock()):
                return "該整備了"
            self.battle_once(session)
            self.battles += 1
            self.crowded_total = 0
            session.request_roles()
            if not session.world.roles.ready():
                raise ProtocolError("戰鬥後隊伍不完整")
            if session.world.map_id != self.state.zone_maps.get(index):
                raise ProtocolError("戰鬥後不在練功地圖（可能全滅被送回記錄點）")
            self.refresh(session)
            self.report_team(session)
            if self.raising and self.battles % LOYALTY_RETRY_BATTLES == 0:
                session._ride_refused.clear()        # 每隔幾場重試一次優先騎寵：騎得上就換過去
                session.ensure_pets()
                self.note_loyalty(session)
                if not self.raising:
                    self.state.retreat_until = 0.0
                    self.state.zone_maps.pop(0, None)
                    return "騎寵忠誠夠了"
            self.state.consecutive_heals = 0
            if self.battles % 5 == 0:
                self.say(f"累計 {self.battles} 場：全隊最低等級 {self.state.minimum_level}，最低血量 {self.state.hp_percent}%")
        return "本輪場次到"

    # ---- 主迴圈
    def run(self) -> int:
        party_waits = 0
        # 啟動時先整備再組隊：整備要讓子號單獨登入，之後得等它們離線；先做就只需要等一輪。
        try:
            self.maintain()
            self.premaintained = True
            self.say("啟動整備完成，接著組隊")
            self.sleep(3.0)
        except (ProtocolError, OSError) as error:
            self.say(f"啟動整備未完成（{error}），改在組隊後再整備")
        while True:
            if self.clock() >= self.deadline:
                self.say("已達總時間上限，停止")
                self.save_status("stop", "已達總時間上限")
                return 0
            # 先連線、組隊、取得狀態；整備需要離線所以在連線之外做。
            try:
                with self.connect() as session:
                    try:
                        self.party(session)
                    except (ProtocolError, OSError) as error:
                        if "驗證" in str(error):
                            self.state.verification = True
                            raise
                        party_waits += 1
                        if party_waits > PARTY_WAIT_LIMIT:
                            self.say(f"組隊一直不成功（{error}），停止")
                            self.save_status("stop", f"組隊失敗：{error}")
                            return 2
                        self.say(f"組隊未完成（{error}），{PARTY_WAIT_SECONDS:.0f} 秒後再試（第 {party_waits} 次）")
                        session.close()
                        stuck = any(text in str(error) for text in (STUCK_IN_BATTLE_TEXT, ROSTER_MISMATCH_TEXT)) and not self.reset_done
                        if (stuck or party_waits == PARTY_RESET_AT) and "殘留狀態" not in str(error):
                            # 2026-11-18 實測：子號可能卡在「战斗中，无法召唤」的殘留狀態，由隊長下指令也解不開；
                            # 用它自己的帳號登入一次就會清掉。只在第 PARTY_RESET_AT 次失敗時做一次。
                            self.reset_members()
                            self.reset_done = True
                        self.sleep(PARTY_WAIT_SECONDS)
                        continue
                    party_waits = 0
                    self.reset_done = False
                    if self.state.last_maintained_level is None and self.premaintained:
                        self.state.last_maintained_level = self.state.minimum_level
                        self.state.last_maintained_at = self.clock()
                    if session.battle.active:
                        # 接手殘留戰鬥：上一圈出事或血量已低就逃跑（這些練功圖脫離戰鬥後會全隊補滿），否則打完。
                        if self.state.flee or self.state.hp_percent < HEAL_BELOW:
                            danger = self.state.hp_percent < HEAL_BELOW
                            current = next((i for i, m in self.state.zone_maps.items() if m == self.state.map_id), None)
                            rounds = session.escape_battle()
                            self.say(f"殘留戰鬥已逃跑脫離（{rounds} 輪）")
                            if danger and current is not None and note_trouble(self.state, current, self.clock()):
                                self.say(f"{ROUTE[current][0]} 打不過，退回 {ROUTE[current - 1][0]} 一段時間")
                        else:
                            self.finish_battle(session)
                        session.request_roles()
                        self.refresh(session)
                    self.state.flee = False
                    task = self.quest_request()
                    if task is not None:
                        self.run_quest(session, task)
                        self.state.failures = 0
                        continue
                    self.raising_low = bool(self.raising) and bool(getattr(session, "_training_low", False))
                    if self.raising_low:
                        # 練忠誠的寵物等級比人物低很多時（1 級的騎寵），借用退圖機制把練功點壓在路線第 0 張：
                        # 阿布的洞穴（龜洞）。使用者 2026-11-21：去低等區練布林帖斯，寵物領好後就去龜洞練。
                        self.state.retreat_cap, self.state.retreat_until = 0, self.clock() + 120.0
                    elif self.state.zone_maps.get(0) == GATE_MAP and (self.state.minimum_level or 1) >= self.gate_until:
                        self.state.zone_maps.pop(0)      # 村口練夠了：下一步改傳送到路線的第一張圖
                    action, detail = decide(self.state, target_level=self.target_level, now=self.clock())
                    self.save_status(action, detail)
                    if action in ("stop", "done"):
                        self.say(("完成：" if action == "done" else "停止：") + str(detail))
                        return 0 if action == "done" else 2
                    if action == "heal":
                        self.say(f"補血：{detail}")
                        zone = allowed_zone(self.state.minimum_level)
                        self.heal(session)
                        self.state.consecutive_heals += 1
                        if note_trouble(self.state, zone, self.clock()):
                            self.say(f"{ROUTE[zone][0]} 補血太頻繁，退回 {ROUTE[zone - 1][0]} 一段時間")
                        self.say(f"補血後最低血量 {self.state.hp_percent}%")
                    elif action == "travel":
                        self.travel(session, detail)
                    elif action == "train":
                        reason = self.train(session, detail)
                        # 這一輪打完就取消原地遇敵（使用者 2026-11-20：配點前要先取消）：接下來可能要離線整備
                        session._send_command(202, (18,), time.monotonic() + session.timeout)
                        self.say(f"本輪結束（{reason}）：累計 {self.battles} 場，全隊最低等級 {self.state.minimum_level}，"
                                 f"最低血量 {self.state.hp_percent}%")
                    if action != "maintain":
                        self.state.failures = 0
                        continue
                    session._send_command(202, (18,), time.monotonic() + session.timeout)      # 要去配點了：先取消原地遇敵
            except (ProtocolError, OSError) as error:
                text = str(error)
                if "驗證" in text:
                    self.say("出現人工驗證，立刻停止，請使用者處理")
                    self.save_status("stop", "人工驗證")
                    return 9
                self.state.failures += 1
                if CROWDED_TEXT in text:
                    self.crowded_total += 1
                    self.crowded = self.crowded_total
                zone = allowed_zone(self.state.minimum_level or 1)
                if self.state.map_id is not None and self.state.map_id in self.state.zone_maps.values():
                    # 在練功圖上出事：下一圈若還在戰鬥中先逃跑。是否算「打不過」由下一圈接手時的血量決定，
                    # 連線逾時、封包過多這類小問題不計入退圖（2026-11-18：兩次小問題就誤退到五兄弟山寨）。
                    self.state.flee = True
                self.say(f"這一圈失敗（第 {self.state.failures} 次）：{text}")
                self.save_status("error", text)
                if self.state.failures >= MAX_FAILURES:
                    self.say("連續失敗達上限，停止")
                    return 2
                self.sleep(30.0)
                continue
            # 到這裡只有 maintain：隊長已離線
            try:
                self.sleep(3.0)
                self.maintain()
                self.state.last_maintained_level = self.state.minimum_level
                self.state.last_maintained_at = self.clock()
                self.say("整備完成，重新組隊")
                self.sleep(3.0)
            except (ProtocolError, OSError) as error:
                self.state.failures += 1
                self.say(f"整備失敗（第 {self.state.failures} 次）：{error}")
                if self.state.failures >= MAX_FAILURES:
                    self.save_status("stop", f"整備失敗：{error}")
                    return 2
                self.sleep(PARTY_WAIT_SECONDS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--map-dir", type=Path, required=True, help="客戶端 map 目錄（唯讀，補血出村路線用）")
    parser.add_argument("--target-level", type=int, default=131)
    parser.add_argument("--maximum-hours", type=float, default=8.0, help="總時間上限（小時）")
    parser.add_argument("--ratio", default="10,9,0,5", help="配點比例：體力,腕力,耐力,敏捷")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if config.get("test_only") is not True:
        print("只接受測試帳號設定")
        return 2
    if not 1 <= args.target_level <= 200 or not 0 < args.maximum_hours <= 24:
        print("目標等級或時間上限超出範圍")
        return 2
    if not (args.map_dir / "1000.dat").is_file():
        print("找不到地圖檔 1000.dat")
        return 2
    ratio = tuple(int(part) for part in args.ratio.split(","))
    supervisor = Supervisor(config, map_dir=args.map_dir, target_level=args.target_level,
                            maximum_seconds=args.maximum_hours * 3600, ratio=ratio,
                            log=lambda message: print(message, flush=True))
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
