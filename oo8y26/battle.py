"""戰鬥名冊與回合確認；每個行動只能送出一次，禁止因重複推送重送。"""
from dataclasses import dataclass, field

from .session import ProtocolError


@dataclass(frozen=True)
class Combatant:
    position: int
    name: bytes = field(repr=False)
    level: int
    hp: int
    max_hp: int
    mount_name: bytes = field(repr=False)
    mount_hp: int
    mount_max_hp: int


@dataclass
class BattleState:
    active: bool = False
    own_position: int | None = None
    round: int | None = None
    ready_mask: int = 0
    awaiting_round: bool = True
    window_id: int = 0
    result_received: bool = False
    combatants: dict = field(default_factory=dict, repr=False)
    sent: set = field(default_factory=set, repr=False)
    pending_initial: int | None = None

    def apply(self, message):
        parts = message.split(b"|")
        tag = parts[0]
        try:
            if tag == b"BP":
                if len(parts) != 4:
                    raise ValueError
                position, flags, limit = [int(v, 16) for v in parts[1:]]
                if not 0 <= position < 10 or not 0 <= flags <= 0xFFFF or not 1 <= limit <= 2000:
                    raise ValueError
                self.active, self.own_position = True, position
                self.combatants = {}
                self.awaiting_round = True
                self.window_id += 1
            elif tag == b"BC":
                if parts[-1] != b"" or (len(parts) - 3) % 22:
                    raise ValueError
                resumed = not self.active
                if resumed:
                    # 登入時接到進行中的戰鬥：伺服器只補送名冊（BC），沒有開場的 BP（2026-11-22 01 的二號，
                    # 換版時被留在戰鬥裡，開局整備一登入就因此停了）。當成戰鬥進行中，自己的位置等名冊解完再補。
                    self.active, self.own_position = True, None
                    self.combatants = {}
                    self.awaiting_round = True
                    self.window_id += 1
                count = (len(parts) - 3) // 22
                if not 1 <= count <= 20:
                    raise ValueError
                actors = {}
                for start in range(2, len(parts) - 1, 22):
                    row = parts[start:start + 22]
                    position, level, hp, maximum = [int(row[i], 16) for i in (0, 4, 5, 6)]
                    mount_hp, mount_maximum = [int(row[i], 16) for i in (11, 12)]
                    if position in actors or not 0 <= position < 20 or not row[1] or not 1 <= level <= 1000:
                        raise ValueError
                    if not 0 <= hp <= maximum <= 10000000 or maximum == 0 or not 0 <= mount_hp <= mount_maximum <= 10000000:
                        raise ValueError
                    actors[position] = Combatant(position, row[1], level, hp, maximum, row[9], mount_hp, mount_maximum)
                self.combatants = actors
                if resumed:
                    self.own_position = min((p for p in actors if p < 5), default=0)
            elif tag == b"BA":
                if len(parts) != 4 or parts[-1] != b"" or not self.active or not self.combatants:
                    raise ValueError
                mask, round_number = int(parts[1], 16), int(parts[2], 16)
                if not 0 <= mask < 1 << 20 or not 0 <= round_number <= 10000:
                    raise ValueError
                if self.round is not None and round_number not in (self.round, self.round + 1):
                    raise ValueError
                self.round, self.ready_mask = round_number, mask
                self.awaiting_round = False
                if self.pending_initial is not None:
                    self.sent.add((round_number, self.pending_initial))
                    self.pending_initial = None
            # BX 為實機觀察到的動畫腳本前綴（BX|位置|BH…），與 BH／BY 同屬不需解析的演出資料。
            # BT：捕捉演出（2026-11-21 抓 1 級布林帖斯時實測 `BT|a0|r10|f0|BY|…`，人物先下捕捉時它排在最前面）
            # BB：遠距離攻擊演出（2026-11-23 玄黃洞窟的盜賊先手時實測 `BB|a10|w0|r0|f20|d0|p0|FF|BB|…|bg|12|BH|`）
            elif tag not in (b"BH", b"BY", b"BX", b"BB", b"BE", b"BT", b"bn", b"bg"):   # BE：逃跑演出（單人逃跑實測 BE|e0|f1|）
                raise ValueError
        except (ValueError, IndexError):
            raise ProtocolError("戰鬥名冊或回合資料異常，已停止（原文開頭：%s）"
                                % message[:60].decode("gbk", "replace")) from None

    def claim(self, position):
        if self.awaiting_round or self.round is None or position not in self.combatants or not 0 <= position < 10:
            raise ProtocolError("尚未確認可控制的戰鬥位置，已停止")
        key = self.round, position
        if self.ready_mask & (1 << position) or key in self.sent:
            raise ProtocolError("本回合行動已送出或已確認，禁止重送")
        self.sent.add(key)

    def claim_initial(self, position):
        """重登入時先選一次行動，由首次 BA 回覆取得真實回合編號。"""
        if self.round is not None or self.pending_initial is not None or position not in self.combatants:
            raise ProtocolError("首次戰鬥行動不可重複選擇，已停止")
        self.pending_initial = position

    def project_party(self, world):
        """BC 在每回合開始更新血量；須核對角色名與指定寵物，不能只靠位置猜。"""
        from .training import allowed_pets, mount_optional, pet_name
        locked_mount, locked_fighter = allowed_pets()
        optional = mount_optional()
        if self.own_position != 0:
            raise ProtocolError("目前僅支援隊長位於第一格的戰鬥，已停止")
        for position in range(5):
            role = world.roles.roles.get(position + 1)
            human, fighter = self.combatants.get(position), self.combatants.get(position + 5)
            actor = world.actor(position + 1)
            if role is None or human is None or fighter is None or human.name != role.name:
                raise ProtocolError("戰鬥角色與指定五人隊伍不符，已停止")
            unmounted = optional and not human.mount_name      # 暫時沒有騎得上的寵物（轉生後等級太低）
            if pet_name(fighter.name) not in locked_fighter or (
                    not unmounted and pet_name(human.mount_name) not in locked_mount):
                raise ProtocolError("戰鬥中指定騎寵或戰寵不符，已停止")
            mounts = ([-1] if unmounted else
                      [pet.index for pet in actor.pets.values() if pet_name(pet.name) == pet_name(human.mount_name)])
            fighters = [pet.index for pet in actor.pets.values() if pet_name(pet.name) == pet_name(fighter.name)]
            if len(mounts) != 1 or len(fighters) != 1 or mounts[0] == fighters[0]:
                raise ProtocolError("戰鬥寵物索引無法唯一確認，已停止")
            # 重登入戰鬥時不能切換寵物；以已校驗 BC 與本機寵物名冊交叉確認。
            actor.mount_pet_index, actor.battle_pet_index = mounts[0], fighters[0]
            actor.hp, actor.max_hp = human.hp, human.max_hp
            actor.level = human.level
            pet = actor.pets[actor.battle_pet_index]
            if not unmounted:
                mount = actor.pets[actor.mount_pet_index]
                mount.hp, mount.max_hp = human.mount_hp, human.mount_max_hp
            pet.hp, pet.max_hp = fighter.hp, fighter.max_hp
