"""自動遊玩的功能模組：每個功能（自動戰鬥、快速戰鬥、自動補血、自動丟肉、自動吃肉……）是一個模組，
「活動」（練功、任務）在 data/module_profiles.json 裡列出要開哪些模組與各自的設定。

    python -X utf8 -u -m oo8y26.modules --list
    python -X utf8 -u -m oo8y26.modules 練功 --config config/query.local.json --map-dir <客戶端 map 目錄>
    python -X utf8 -u -m oo8y26.modules 任務 --task 64 --config config/query.local.json --map-dir <客戶端 map 目錄>

練功的一輪：
1. 收尾：每隻角色單獨登入一次——把殘留的戰鬥打完、回記錄點（各模組可以順便做自己的事，例如領智慧果）。
2. 整備：各模組的離線工作（配點、吃智慧果）。
3. 進場：組隊 → 貼心傳送到練功點 → 各模組進場設定（開快戰）。
4. 每一場：戰前（清背包、遇敵）→ 戰鬥 → 戰後（吃肉、補血、血量檢查）。
5. 打滿「每輪場數」就斷線回到第 1 步（每條連線有命令數上限保護，也順便重新整備）。
任何一步出錯就停（raise ProtocolError），不自動重試。
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import allocate as allocate_module
from . import panels, quest
from .session import ProtocolError, Session

PROFILE_FILE = Path(__file__).resolve().parent.parent / "data" / "module_profiles.json"
MAX_MEALS = 10             # 一次吃肉補血最多吃幾個
MAX_ROUNDS = 400           # 一次執行最多幾輪（上限保護）


@dataclass
class Context:
    """一輪練功裡各模組共用的狀態。"""
    config: dict
    map_dir: Path
    say: object = print
    settings: dict = field(default_factory=dict)      # 活動本身的設定（傳送、目標等級……）
    session: Session | None = None
    fought: int = 0                                   # 這一輪打了幾場
    end_round: bool = False                           # 模組要求結束這一輪（例如回村補血之後要重新傳送）
    spans: list = field(default_factory=list)         # 每場的（秒數, 回合數）


class Module:
    """功能模組的基底。defaults 是可調的設定與預設值；needs 是必須一起開的模組名稱。"""
    name = ""
    summary = ""
    defaults: dict = {}
    needs: tuple = ()

    def __init__(self, options=None):
        options = dict(options or {})
        unknown = set(options) - set(self.defaults)
        if unknown:
            raise ValueError(f"模組「{self.name}」沒有這些設定：{'、'.join(sorted(unknown))}")
        self.options = {**self.defaults, **options}

    def settle(self, context, session, sub):      # 收尾時每隻角色單獨登入的那條連線
        pass

    def prepare(self, context):                   # 離線整備
        pass

    def assemble(self, context):                  # 一號上線後、傳送前
        pass

    def enter(self, context):                     # 到練功點之後
        pass

    def before_battle(self, context):
        pass

    def fight(self, context):                     # 回傳回合數；不負責戰鬥的模組回 None
        return None

    def after_battle(self, context):
        pass


MODULES: dict[str, type] = {}


def register(cls):
    MODULES[cls.name] = cls
    return cls


def _pump(session, seconds, step=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        session.poll_event(step)


def connect(config: dict, sub: int) -> Session:
    session = Session(timeout=20)
    session.authenticate(config["host"], int(config["port"]), config["account"], str(sub),
                         config["subaccounts"][str(sub)]["password_md5"], server_index=int(config["server_index"]))
    session.request_characters()
    session.select_character()
    session.read_initial_world()
    session.sync()
    session.request_roles()      # 第一次查名冊時伺服器才會補送進行中的戰鬥；不查的話會以為沒在打
    _pump(session, 3.0)
    return session


# ---------------------------------------------------------------- 整備類
@register
class AutoParty(Module):
    name = "自動組隊"
    summary = "一號把隊員下線、重新叫上線、召喚到身邊並組隊"
    defaults = {"隊員": [2, 3, 4, 5]}

    def assemble(self, context):
        quest.reform(context.session, 1, tuple(self.options["隊員"]), say=context.say, summon=True)


@register
class AutoAllocate(Module):
    name = "自動配點"
    summary = "每隻角色單獨登入，把升級點數依比例配到體力／腕力／耐力／敏捷"
    defaults = {"比例": [10, 9, 0, 5]}

    def prepare(self, context):
        for sub in range(1, 6):
            result = allocate_module.allocate(context.config, sub, tuple(self.options["比例"]))
            context.say(f"角色 {sub} 配點：原有 {result['points']} 點，生命上限 {result['hp'][1]}")
            time.sleep(2.0)


@register
class AutoWisdomFruit(Module):
    name = "自動吃智慧果"
    summary = "經驗加成到期就再吃一顆；身上沒有就先從個人道具倉庫領出"
    defaults = {"道具": "双倍智慧果(8小时)", "確認訊息": "学习经验的能力提升"}

    def settle(self, context, session, sub):
        """身上沒有智慧果、而個人道具倉庫裡有，就領一組出來。"""
        world, fruit = session.world, self.options["道具"]
        if world.has_item(fruit) or world.free_bag_slots() < 1:
            return

        def ask(data, prefix):
            session._send_command(821, (2, data), time.monotonic() + 20)
            end = time.monotonic() + 6
            while time.monotonic() < end:
                event = session.poll_event(0.3)
                if (event is not None and event.func == 820 and event.values[0] == 2
                        and event.values[1].startswith(prefix)):
                    return event.values[1].decode("gbk", "replace").split("|")
            return None

        listing = ask(b"O|", b"L|")
        if listing is None:
            return
        for key in [listing[index + 3] for index in range(4, len(listing) - 5, 6)]:
            time.sleep(1.5)
            info = ask(f"X|{key}|".encode(), b"I|")
            if info is not None and len(info) > 2 and info[2] == fruit:
                time.sleep(1.5)
                if ask(f"B|{key}|".encode(), b"D|") is not None:
                    context.say(f"角色 {sub}：從個人道具倉庫領出{fruit}")
                return

    def prepare(self, context):
        from . import newbie
        from .supervisor import Supervisor
        reader = Supervisor(context.config, map_dir=context.map_dir, target_level=0, maximum_seconds=0.0)
        lefts = []
        for sub in range(1, 6):
            left = reader.fruit_left(sub)
            time.sleep(2.0)
            if left == 0:
                session, outfitter = newbie._connect(context.config, sub)
                with session:
                    outfitter.load()
                    if outfitter.use_expecting(self.options["道具"], self.options["確認訊息"]):
                        context.say(f"角色 {sub}：智慧果已到期，再吃一顆")
                        left = 8 * 3600
                    else:
                        context.say(f"角色 {sub}：智慧果已到期，但背包沒有智慧果")
                time.sleep(2.0)
            lefts.append(left)
        known = [left for left in lefts if left]
        if known:
            context.say(f"智慧果最早 {min(known) // 60} 分鐘後到期（讀到 {len(known)}／5 隻）")


# ---------------------------------------------------------------- 戰鬥類
@register
class AutoEncounter(Module):
    name = "自動遇敵"
    summary = "原地遇敵開打（C202 17），開打後取消後續遇敵（C202 18）"
    defaults = {"補送上限": 5}

    def __init__(self, options=None):
        super().__init__(options)
        self.misses = 0

    def before_battle(self, context):
        session, seen = context.session, []
        for _ in range(2):           # 偶爾送出遇敵後沒有開打：只補送一次，累計超過上限就停
            session._send_command(202, (17,), time.monotonic() + 20)
            end = time.monotonic() + 10
            while not session.battle.active and time.monotonic() < end:
                event = session.poll_event(0.2)
                if event is not None:
                    seen.append((event.func, repr(event.values)[:120]))
            if session.battle.active:
                break
            self.misses += 1
            context.say(f"送出遇敵後沒有開打（第 {self.misses} 次）；期間收到：{seen[-6:]}")
            if self.misses > self.options["補送上限"]:
                break
        if not session.battle.active:
            raise ProtocolError("沒有進入戰鬥，已停止")
        session._send_command(202, (18,), time.monotonic() + 20)


@register
class AutoBattle(Module):
    name = "自動戰鬥"
    summary = "戰場上的人物、寵物全部普通攻擊第一個還站著的敵人，打到結束（每回合送 ZD／DD 同步）"

    def fight(self, context):
        return quest.fight_present(context.session, say=lambda message: None, allow_escape=False)


def fast_fight(session) -> int:
    """快戰打法（2026-11-20 用真實客戶端錄到的做法）：開了快戰（KS）之後客戶端不再送回合同步的 ZD／DD，
    每回合直接把指令送出去。這裡照做：一回合的十個指令連續送出、不逐一等確認，再等回合推進。
    回合 6 秒內沒有推進就改用一般流程（quest.fight_plain）把這場打完。回傳回合數。"""
    battle = session.battle
    ended = {"value": False}

    def wait(seconds, done):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = session.poll_event(0.05)
            if event is not None and (event.func == 101 or (event.func == 7 and event.values[0] == 0)):
                ended["value"] = True
            if ended["value"] or done():
                return True
        return ended["value"] or done()

    for rounds in range(60):
        wait(8.0, lambda: bool(battle.combatants) and not battle.awaiting_round)
        if ended["value"]:
            break
        enemies = [unit for position, unit in sorted(battle.combatants.items()) if position >= 10 and unit.hp > 0]
        if not enemies:
            wait(6.0, lambda: any(position >= 10 and unit.hp > 0 for position, unit in battle.combatants.items()))
            if ended["value"]:
                break
            continue
        target = enemies[0].position
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
        battle.awaiting_round = True
        current = battle.round
        if not wait(6.0, lambda: not battle.awaiting_round and battle.round != current) and not ended["value"]:
            return rounds + quest.fight_plain(session, say=lambda message: None, allow_escape=False)
        if ended["value"]:
            break
    else:
        raise ProtocolError("戰鬥回合數超過上限，已停止")
    wait(3.0, lambda: ended["value"])
    if not ended["value"]:
        raise ProtocolError("戰鬥沒有收到結束通知，已停止")
    battle.active = False
    return rounds + 1


@register
class FastBattle(Module):
    name = "快速戰鬥"
    summary = "進場時打開快戰開關（KS），戰鬥改用快戰打法（不送 ZD／DD，每場約 5.5 秒 → 2.3 秒）"
    needs = ("自動戰鬥",)

    def enter(self, context):
        session = context.session
        session._send_command(2025, (f"KS|{session.sock.fileno()}|1|".encode(),), time.monotonic() + 20)
        _pump(session, 1.5)

    def fight(self, context):
        return fast_fight(context.session)


# ---------------------------------------------------------------- 背包與血量
def meat_slot(world):
    return next((slot for slot in sorted(world.items or {})
                 if 9 <= slot <= 23 and world.items[slot][0].endswith("肉")), None)


def weakest(world) -> tuple:
    """一號自己和騎寵、戰寵裡血量百分比最低的：（百分比, 使用道具的對象編號）。對象 0＝自己、寵物＝欄位＋1。"""
    units = [(world.hp * 100 // world.max_hp if world.hp is not None and world.max_hp else 100, 0)]
    for index in (world.mount_pet_index, world.battle_pet_index):
        pet = world.pets.get(index) if index is not None else None
        if pet is not None and pet.hp is not None and pet.max_hp:
            units.append((pet.hp * 100 // pet.max_hp, index + 1))
    return min(units)


def eat_meat(session, below: int, *, say=print) -> int:
    """一號或牠的寵物血量低於 below% 時，把背包裡的肉餵給血最少的那個，直到都不低於 below% 或沒有肉。
    每吃一個都等伺服器回報道具減少；伺服器不受理就停。回傳吃了幾個。"""
    world, eaten = session.world, 0
    while eaten < MAX_MEALS:
        percent, target = weakest(world)
        slot = meat_slot(world)
        if percent >= below or slot is None:
            break
        before = world.items[slot]
        time.sleep(quest.EAT_SECONDS)
        quest._send_item(session, 17, (world.x, world.y, slot, target))
        if not quest._item_changed(session, slot, before, 3.0):
            raise ProtocolError("吃肉伺服器沒有受理，已停止")
        _pump(session, 0.6)
        eaten += 1
    if eaten:
        say(f"吃了 {eaten} 個肉補血，目前最低 {weakest(world)[0]}%")
    return eaten


@register
class AutoDropMeat(Module):
    name = "自動丟肉"
    summary = "戰前背包空格不夠就把肉丟到地上；地上放不下才改成吃掉（同遊戲面板的「丟棄或吃掉任何種類肉」）"
    defaults = {"最少空格": 3, "清到空格": 6}

    def settle(self, context, session, sub):
        # 隊員的背包只有牠自己登入時清得到（2026-11-20 查面板才發現四隻隊員的背包都被肉塞滿）。
        if session.world.items is not None and session.world.free_bag_slots() < self.options["最少空格"]:
            quest.make_room(session, self.options["清到空格"], say=lambda message: context.say(f"角色 {sub}：{message}"))

    def before_battle(self, context):
        if context.session.world.free_bag_slots() < self.options["最少空格"]:
            quest.make_room(context.session, self.options["清到空格"], say=lambda message: None)


@register
class AutoEatMeat(Module):
    name = "自動吃肉"
    summary = "戰後一號或牠的寵物血量低於門檻時，吃背包裡的肉補血"
    defaults = {"門檻": 80}

    def after_battle(self, context):
        eat_meat(context.session, self.options["門檻"], say=context.say)


def lowest_hp(world) -> int:
    """全隊（人物、騎寵、戰寵）最低血量百分比；隊員資料不完整時只看一號和牠的寵物。"""
    from .supervisor import team_hp_percent
    try:
        return team_hp_percent(world)
    except ProtocolError:
        return quest.solo_hp_percent(world)


@register
class AutoHeal(Module):
    name = "自動補血"
    summary = "戰後血量低於補血門檻就補（村口：全隊回薩村村口打一場補滿；道具：一號用免費烤豬排）；補完仍低於停止門檻就停"
    defaults = {"補血門檻": 50, "停止門檻": 30, "方式": "村口"}

    def __init__(self, options=None):
        super().__init__(options)
        if self.options["方式"] not in ("村口", "道具"):
            raise ValueError("自動補血的方式只能是「村口」或「道具」")

    def after_battle(self, context):
        session = context.session
        world = session.world
        if lowest_hp(world) < self.options["補血門檻"]:
            if self.options["方式"] == "道具":
                quest.heal_with_items(session, say=context.say)
            else:
                # 隊長回記錄點時隊員不會跟著走，所以回去後重新召喚，再一起走到村口打一場（那張圖戰後全隊補滿）。
                # 補完結束這一輪，下一輪照正常流程重新組隊、傳送回練功點。
                context.say(f"血量最低 {lowest_hp(world)}%，回村口補血")
                session.return_to_record()
                party = context.settings.get("隊員", (2, 3, 4, 5))
                quest.reform(session, 1, tuple(party), say=context.say, summon=True)
                quest.heal_solo(session, context.map_dir, say=context.say)
                session.request_roles()
                context.end_round = True
        if world.hp * 100 < world.max_hp * self.options["停止門檻"]:
            raise ProtocolError(f"一號血量只剩 {world.hp}/{world.max_hp}，已停止")


# ---------------------------------------------------------------- 設定檔
def load_profiles(path: Path = PROFILE_FILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build(enabled: dict) -> list:
    """把「模組名稱 → 設定」變成模組物件（照設定檔的順序）；名稱不存在或缺少相依模組就 raise。"""
    unknown = [name for name in enabled if name not in MODULES]
    if unknown:
        raise ValueError(f"沒有這些模組：{'、'.join(unknown)}（可用：{'、'.join(MODULES)}）")
    modules = [MODULES[name](options) for name, options in enabled.items()]
    for module in modules:
        missing = [name for name in module.needs if name not in enabled]
        if missing:
            raise ValueError(f"模組「{module.name}」需要同時開啟：{'、'.join(missing)}")
    return modules


def fighter_of(modules: list):
    """負責戰鬥的模組：快速戰鬥優先，其次自動戰鬥。"""
    for name in ("快速戰鬥", "自動戰鬥"):
        for module in modules:
            if module.name == name:
                return module
    raise ValueError("練功至少要開「自動戰鬥」")


# ---------------------------------------------------------------- 活動：練功
def levels(world) -> dict:
    return {slot: world.roles.roles[slot].level for slot in range(1, 6)}


def settle(context, modules, connect=connect) -> None:
    """每隻角色單獨登入一次：殘留的戰鬥打完、回記錄點，再讓各模組做收尾（領智慧果）。
    人留在練功點會被怪找上，之後單獨登入配點會碰到「角色在戰鬥中」，所以先回村。"""
    for sub in range(1, 6):
        with connect(context.config, sub) as session:
            if session.battle.active:
                quest.fight_present(session, say=lambda message: None, allow_escape=False)
            if session.world.map_id != quest.RECORD_MAP:
                session.return_to_record()
                time.sleep(1.0)
            for module in modules:
                module.settle(context, session, sub)
            panels.fetch_currency(session)
            panels.record(sub, session.world)        # 單獨登入時才收得到道具與任務：順便更新面板快照


def train_round(context, modules, remaining: int, connect=connect) -> bool:
    """打一輪（到每輪場數、remaining 用完、或模組要求結束為止）；回傳全隊是否到了目標等級。"""
    settings, say = context.settings, context.say
    fighter = fighter_of(modules)
    context.fought, context.end_round = 0, False
    with connect(context.config, 1) as session:
        context.session = session
        world = session.world
        if session.battle.active:
            quest.fight_present(session, say=lambda message: None, allow_escape=False)
        if world.map_id != quest.RECORD_MAP:
            session.return_to_record()
        for module in modules:
            module.assemble(context)
        say(f"傳送 → {session.teleport(int(settings['傳送']))}")
        spot = settings.get("站位")
        if spot and (world.x, world.y) != tuple(spot):
            quest.walk(session, context.map_dir, tuple(spot), say=lambda message: None,
                       fight=lambda current: quest.fight_present(current, say=lambda message: None, allow_escape=False))
        for module in modules:
            module.enter(context)
        session.request_roles()
        say(f"練功點 ({world.x},{world.y})；等級 {levels(world)}；一號經驗欄位 {getattr(world, 'exp_fields', None)}")
        while context.fought < min(remaining, int(settings["每輪場數"])) and not context.end_round:
            for module in modules:
                module.before_battle(context)
            began = time.monotonic()
            rounds = fighter.fight(context)
            context.spans.append((time.monotonic() - began, rounds))
            _pump(session, 2.0)
            for module in modules:
                module.after_battle(context)
                if context.end_round:
                    break
            context.fought += 1
            if context.fought % int(settings.get("回報間隔", 20)) == 0:
                session.request_roles()
                panels.record_team(world)
                now = levels(world)
                recent = context.spans[-20:]
                say(f"已打 {context.fought} 場（最近每場 {sum(s for s, _ in recent) / len(recent):.1f} 秒）；"
                    f"等級 {now}；一號經驗欄位 {getattr(world, 'exp_fields', None)}")
                if min(now.values()) >= int(settings["目標等級"]):
                    say(f"全隊到 {settings['目標等級']} 等，停止")
                    return True
    return False


def run_training(config: dict, map_dir: Path, profile: dict, *, battles: int, say=print) -> int:
    modules = build(profile["模組"])
    fighter_of(modules)
    settings = {key: value for key, value in profile.items() if key != "模組"}
    party = profile["模組"].get("自動組隊", {}).get("隊員")
    if party:
        settings["隊員"] = party
    context = Context(config=config, map_dir=map_dir, say=say, settings=settings)
    say("開啟的模組：" + "、".join(module.name for module in modules))
    done = 0
    with panels.activity("練功"):
        for _ in range(MAX_ROUNDS):
            if done >= battles:
                break
            settle(context, modules)
            for module in modules:
                module.prepare(context)
            reached = train_round(context, modules, battles - done)
            done += context.fought
            say(f"這一輪打了 {context.fought} 場，累計 {done} 場")
            if reached:
                break
    say("練功結束")
    return 0


# ---------------------------------------------------------------- 活動：任務
def quest_settings(profile: dict, task: int) -> dict:
    """任務的設定＝共用設定，再蓋上「各任務」裡這個任務自己的限制。"""
    settings = {key: value for key, value in profile.items() if key not in ("各任務", "說明")}
    settings.update({key: value for key, value in profile.get("各任務", {}).get(str(task), {}).items()
                     if key != "備註"})
    return settings


def run_quest(config: dict, map_dir: Path, profile: dict, task: int) -> int:
    settings = quest_settings(profile, task)
    quest.CARAVAN_HEAL_BELOW = int(settings.get("補血門檻", 85))
    quest.CARAVAN_DISCARD = bool(settings.get("丟棄名單道具", True))
    quest.STORE_SPARE_PETS = bool(settings.get("多餘寵物存倉庫", True))
    fast = bool(settings.get("快速戰鬥", False))
    quest.CARAVAN_FAST, quest.FIGHTER = fast, (fast_fight if fast else None)
    quest.CARAVAN_PRE_HEAL = ((lambda session, say: eat_meat(session, quest.CARAVAN_HEAL_BELOW, say=say))
                              if settings.get("先吃肉補血", False) else None)
    quest.CARAVAN_ON_CONNECT = lambda sub, session: panels.record(sub, session.world)
    with panels.activity(f"任務 {task}"):
        return quest.run_caravan(config, map_dir, task, tuple(settings.get("隊伍", (1, 2, 3, 4, 5))))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("activity", nargs="?", help="活動名稱（data/module_profiles.json 裡的鍵，例如 練功、任務）")
    parser.add_argument("--list", action="store_true", help="列出所有模組與各活動開了哪些")
    parser.add_argument("--config", type=Path, default=Path("config/query.local.json"))
    parser.add_argument("--map-dir", type=Path)
    parser.add_argument("--profiles", type=Path, default=PROFILE_FILE)
    parser.add_argument("--battles", type=int, default=6000, help="練功：最多打幾場")
    parser.add_argument("--target", type=int, help="練功：目標等級（蓋過設定檔）")
    parser.add_argument("--teleport", type=int, help="練功：貼心傳送代號（蓋過設定檔）")
    parser.add_argument("--task", type=int, help="任務：任務編號")
    args = parser.parse_args(argv)

    def say(message):
        print(time.strftime("%H:%M:%S"), message, flush=True)

    try:
        profiles = load_profiles(args.profiles)
        if args.list or args.activity is None:
            for name, cls in MODULES.items():
                print(f"{name}：{cls.summary}" + (f"　設定 {cls.defaults}" if cls.defaults else ""))
            for name, profile in profiles.items():
                if isinstance(profile, dict) and "模組" in profile:
                    print(f"活動「{name}」開啟：{'、'.join(profile['模組'])}")
            return 0
        profile = profiles[args.activity]
        if not args.config.is_file():
            raise ValueError(f"找不到帳號設定檔 {args.config}")
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if config.get("test_only") is not True:
            raise ValueError("只接受測試帳號設定")
        line = profiles.get("線路", {})
        config["port"] = line.get("port", config["port"])
        config["server_index"] = line.get("server_index", config["server_index"])
        if args.map_dir is None:
            raise ValueError("要給 --map-dir")
        if not (args.map_dir / "1000.dat").is_file():
            raise ValueError(f"地圖目錄 {args.map_dir} 裡找不到 1000.dat")
        if profile.get("類型") == "任務":
            if args.task is None:
                raise ValueError("任務活動要給 --task")
            return run_quest(config, args.map_dir, profile, args.task)
        profile = dict(profile)
        if args.target is not None:
            profile["目標等級"] = args.target
        if args.teleport is not None:
            profile["傳送"] = args.teleport
        return run_training(config, args.map_dir, profile, battles=args.battles, say=say)
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        say(f"停止：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
