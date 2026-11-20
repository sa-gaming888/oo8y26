"""依全隊最低等級選擇練功階段，並在戰鬥前檢查寵物、地圖與生命狀態。"""
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import time

from .session import ProtocolError


@dataclass(frozen=True)
class TrainingPlan:
    config: dict = field(repr=False)

    @classmethod
    def load(cls, path=None):
        # 不同隊伍可用環境變數 oo8y26_TRAINING_PLAN 指定自己的計畫檔（例如戰寵不同）。
        override = os.environ.get("oo8y26_TRAINING_PLAN")
        path = Path(path or override) if (path or override) else (
            Path(__file__).resolve().parents[1] / "data" / "training_plan.json")
        config = json.loads(path.read_text(encoding="utf-8"))
        thresholds = [stage["minimum_team_level"] for stage in config["stages"]]
        if not thresholds or thresholds[0] != 1 or thresholds != sorted(set(thresholds)):
            raise ValueError("練功階段等級必須從 1 開始且嚴格遞增")
        limits = config["limits"]
        if not 0 < limits["maximum_seconds"] <= 86400 or not 0 < limits["maximum_battles"] <= 10000:
            raise ValueError("練功時間或場次上限設定錯誤")
        if not 0 <= limits["minimum_hp_percent"] <= 100:
            raise ValueError("最低生命百分比設定錯誤")
        return cls(config)

    def stage(self, role_book):
        slots = (self.config["leader"],) + tuple(self.config["members"])
        level = role_book.minimum_level(slots)
        eligible = [rule for rule in self.config["stages"] if level >= rule["minimum_team_level"]]
        if not eligible:
            raise ProtocolError("找不到符合隊伍等級的練功路線，已停止")
        return eligible[-1]

    def ready_to_fight(self, world):
        return self._validate_party(world, confirm_battle_pet=True)

    def prepare_encounter(self, world):
        """遇敵前可先確認戰寵存在與血量，實際出戰索引須在 BC 後再次驗證。"""
        return self._validate_party(world, confirm_battle_pet=False)

    def _validate_party(self, world, *, confirm_battle_pet):
        if not world.roles.ready(self.config["leader"], tuple(self.config["members"])):
            raise ProtocolError("隊伍尚未完整，禁止開始戰鬥")
        rule = self.stage(world.roles)
        if rule["map_id"] is None or world.map_id != rule["map_id"]:
            raise ProtocolError("練功地圖尚未驗證或尚未抵達，禁止開始戰鬥")
        for slot in (self.config["leader"],) + tuple(self.config["members"]):
            actor = world.actor(slot)
            if actor.hp is None or actor.max_hp is None or actor.max_hp <= 0:
                raise ProtocolError("尚未確認全隊生命狀態，禁止開始戰鬥")
            if actor.hp * 100 < actor.max_hp * self.config["limits"]["minimum_hp_percent"]:
                raise ProtocolError("隊員生命低於練功門檻，先補給再戰鬥")
            battle_index = actor.battle_pet_index
            if battle_index is None and not confirm_battle_pet:
                matches = [pet.index for pet in actor.pets.values() if pet_name(pet.name) == locked_pets()[1]]
                if len(matches) != 1:
                    raise ProtocolError("可用戰寵無法唯一確認，禁止遇敵")
                battle_index = matches[0]
            for index, expected in zip((actor.mount_pet_index, battle_index), locked_pets()):
                if index not in actor.pets:
                    raise ProtocolError("尚未確認全隊騎寵或戰寵狀態，禁止開始戰鬥")
                pet = actor.pets[index]
                if pet_name(pet.name) != expected:
                    raise ProtocolError("騎寵或戰寵不符合鎖定設定，禁止開始戰鬥")
                if pet.hp * 100 < pet.max_hp * self.config["limits"]["minimum_hp_percent"]:
                    raise ProtocolError("指定寵物生命低於練功門檻，先補給再戰鬥")
        return rule


def locked_pets() -> tuple:
    """目前練功計畫鎖定的（騎寵, 戰寵）名稱；名稱先經 pet_name 的繁簡統一。"""
    config = TrainingPlan.load().config
    return (pet_name(config["mount"].encode("utf-8")), pet_name(config["battle_pet"].encode("utf-8")))


def pet_name(raw: bytes) -> str:
    """只統一指定名稱的繁簡字；不模糊比對或挑選其他寵物。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding).replace("欧", "歐")
        except UnicodeError:
            pass
    raise ProtocolError("寵物名稱編碼無法確認，已停止")


def run_novice(session, *, maximum_battles=10, maximum_seconds=300, on_progress=None):
    """有場次、時間上限的連續新手練功；需要換圖時先停止下一場。"""
    plan = TrainingPlan.load()
    limits = plan.config["limits"]
    if not 1 <= maximum_battles <= limits["maximum_battles"] or not 30 <= maximum_seconds <= limits["maximum_seconds"]:
        raise ValueError("連續練功上限超出計畫範圍")
    deadline = time.monotonic() + maximum_seconds
    completed = 0
    try:
        session.ensure_party()
        session.ensure_pets()
        for _ in range(maximum_battles):
            rule = plan.stage(session.world.roles)
            # 計畫中還沒有驗證地圖的階段不打；目前所在地圖與階段不符時也先停，由呼叫端轉場。
            if rule["map_id"] is None or session.world.map_id != rule["map_id"]:
                return completed, "全隊已達轉場等級"
            remaining = deadline - time.monotonic()
            if remaining < 30:
                return completed, "已達時間上限"
            # 3~4 級、一次四隻的怪實測要打到 3 回合，回合上限放寬到 8。
            session.test_novice_battle(maximum_rounds=rule.get("maximum_rounds", 8),
                                       maximum_seconds=min(rule.get("maximum_battle_seconds", 120), remaining))
            completed += 1
            session.request_roles()
            if not session.world.roles.ready():
                raise ProtocolError("戰鬥後隊伍不完整，已停止連續練功")
            if on_progress:
                on_progress(completed, session.world)
        return completed, "已達場次上限"
    except BaseException:
        session.close()
        raise
