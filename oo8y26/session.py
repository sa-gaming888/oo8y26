"""有期限與錯誤即關線保護的無 GUI 登入、選角與初始地圖同步。

不自動重試；不輸出憑證、金鑰或原始回包。連線由呼叫端透過 close 或 with 收尾。
"""
from __future__ import annotations

import base64

import socket
import time
import hashlib
import select
from collections import deque
from dataclasses import dataclass, field

from . import crypto
from .client.framing import FrameReader
from .client.greeting import parse_greeting
from .login import build_login_packet, decode_integer, encode_integer, function_offset


class ProtocolError(RuntimeError):
    """協定不符、拒絕登入或校驗錯誤；發生後不得繼續送指令。"""


@dataclass
class CharacterList:
    """角色清單保留原始資料供後續解析，不在 repr 顯示內容。"""

    status: bytes
    roster: bytes = field(repr=False)


@dataclass(frozen=True)
class Character:
    """每個子號的一個角色；原始位元組保留遊戲編碼，不寫入日誌。"""

    name: bytes = field(repr=False)
    numbers: tuple[int, ...] = field(repr=False)


@dataclass(frozen=True)
class WorldRegion:
    """伺服器已下發的地圖區塊；區塊範圍不是角色實際座標。"""

    map_id: int
    x0: int
    y0: int
    x1: int
    y1: int


def decode_fields(payload: bytes, cipher_key: bytes, field_key: bytes, offset: int,
                  schema: str, expected_func: int) -> list:
    """依已確認的 I/S 欄位佈局解碼，驗證每一包的整數校驗和。"""
    if len(payload) < 8 or len(payload) % 4:
        raise ProtocolError("回包密文長度錯誤，已停止")
    try:
        func, fields = crypto.parse_token(crypto.xxtea_decrypt(payload, cipher_key))
        if int(func) != expected_func + offset or len(fields) != len(schema) + 1:
            raise ProtocolError("遊戲回應類型或欄位數不符，已停止")
        values = [decode_integer(value, field_key) if kind == "I" else crypto.field_decode_shl(value, field_key)
                  for value, kind in zip(fields[:-1], schema)]
        total = sum(value if isinstance(value, int) else len(value) for value in values)
        total = ((total + (1 << 31)) % (1 << 32)) - (1 << 31)
        if total != decode_integer(fields[-1], field_key):
            raise ProtocolError("遊戲回應校驗和錯誤，已停止")
        return values
    except (ValueError, AssertionError, KeyError, IndexError):
        raise ProtocolError("遊戲回應解碼失敗，已停止") from None


def parse_character(roster: bytes) -> Character:
    """實機角色清單為 名字|槽位\\z資料…，只接受已驗證的一角色結構。"""
    try:
        name, detail = roster.split(b"|", 1)
        parts = detail.split(b"\\z")
        if not 1 <= len(name) <= 48 or len(parts) != 16:
            raise ValueError
        numbers = tuple(int(value) for value in parts[:14])
        if numbers[0] != 0 or any(not -(1 << 31) <= number < (1 << 31) for number in numbers):
            raise ValueError
        return Character(name, numbers)
    except (ValueError, IndexError):
        raise ProtocolError("角色清單結構尚未支援，已停止") from None


def character_key(character: Character, timestamp: str, account_sub: bytes) -> tuple[bytes, int]:
    """0x7f4a94：角色資料、問候時間與子號推導第二階段金鑰和 func 位移。"""
    n = character.numbers
    message = str(n[2]).encode() + str(n[1]).encode() + timestamp.encode()
    message += str(n[5]).encode() + account_sub + str(n[6]).encode()
    digest = hashlib.md5(message).hexdigest()
    return digest[:16].encode(), int(digest[19] + digest[6] + digest[14], 16) % 60


def decode_string_reply(payload: bytes, cipher_key: bytes, field_key: bytes,
                        offset: int, expected_func: int, string_count: int) -> list[bytes]:
    """檢查指定回應類型、字串欄位數與整數校驗和。"""
    if len(payload) < 8 or len(payload) % 4:
        raise ProtocolError("回包密文長度錯誤，已停止")
    try:
        token = crypto.xxtea_decrypt(payload, cipher_key)
        if not token.startswith(b"&;"):
            raise ProtocolError("伺服器回包標記錯誤，已停止")
        func, fields = crypto.parse_token(token)
        if int(func) != expected_func + offset or len(fields) != string_count + 1:
            raise ProtocolError(f"回應類型或欄位數不符：預期 {expected_func}/{string_count + 1}，"
                                f"收到 {int(func) - offset}/{len(fields)}；已停止")
        values = [crypto.field_decode_shl(value, field_key) for value in fields[:-1]]
        checksum = decode_integer(fields[-1], field_key)
        if sum(map(len, values)) != checksum:
            raise ProtocolError("回應校驗和錯誤，已停止")
        return values
    except (ValueError, AssertionError, KeyError, IndexError):
        raise ProtocolError("回應解碼失敗，已停止；敏感內容已隱藏") from None


HEARTBEAT_VALUE = bytes.fromhex("0d700000")   # C822 的固定值（實機擷取 514/514 相同）
HEARTBEAT_FIRST_SECONDS = 30.0
HEARTBEAT_SECONDS = 30.1


RIDE_TRY_SECONDS = 4.0           # 試騎優先騎寵時等多久；沒騎上就當作忠誠不足
RIDE_RETRY_SECONDS = 1800.0      # 優先騎寵被以忠誠不足拒絕後，隔多久再試一次


class Session:
    """一條無 GUI 遊戲連線；支援帳號驗證、角色清單、選角與初始地圖。"""

    def __init__(self, timeout: float = 10.0):
        if not 0 < timeout <= 30:
            raise ValueError("單一步驟期限必須介於 0 與 30 秒之間")
        self.timeout = timeout
        self.sock = None
        self.authenticated = False
        self._cipher_key = b""
        self._field_key = b""
        self._offset = 0
        self._frames = deque()
        self._reader = FrameReader("server")
        self._account_sub = b""
        self._timestamp = ""
        self.character = None
        self.entered = False
        self.selected = False
        self._barrier_id = 0
        self._command_count = 0
        self._last_control_at = None
        self._controls_initialized = False
        from .world import WorldState
        self.world = WorldState()
        from .battle import BattleState
        self.battle = BattleState()
        self._battle_ends = 0
        self._phase = "尚未開始遊戲操作"
        self._last_command_func = None
        self._last_event_func = None
        self._next_heartbeat = None

    def authenticate(self, host: str, port: int, account: str, sub: str,
                     password_md5: str, *, server_index: int):
        """只建立一次連線、送一次握手；只有 ok 且校驗正確才標記登入成功。"""
        if self.sock is not None:
            raise ProtocolError("已有連線，禁止重複登入")
        # 在建立連線前檢查輸入，不以無效帳密測試伺服器。
        if not account.isascii() or not account.isalnum() or not sub.isascii() or not sub.isdigit():
            raise ValueError("測試帳號與子號格式錯誤")
        if not 1 <= int(sub) <= 5:
            raise ValueError("子號必須介於 1 至 5")
        if len(password_md5) != 32 or any(c not in "0123456789abcdefABCDEF" for c in password_md5):
            raise ValueError("密碼雜湊格式錯誤")
        if not -32768 <= server_index <= 32767:
            raise ValueError("伺服器索引超出範圍")
        deadline = time.monotonic() + self.timeout
        try:
            self.sock = socket.create_connection((host, port), timeout=self.timeout)
            greeting = bytearray()
            # 逐 byte 收到 NUL，避免丟失同一個 TCP 區段後面的框架。
            while len(greeting) < 45:
                self._set_deadline(deadline)
                byte = self.sock.recv(1)
                if not byte:
                    raise ConnectionError("接收問候時連線關閉，已停止")
                greeting += byte
                if byte == b"\x00":
                    break
            if not greeting.endswith(b"\x00"):
                raise ProtocolError("問候超過長度上限，已停止")
            parsed = parse_greeting(bytes(greeting))
            sk, ts = parsed["session_key"], parsed["timestamp"]
            self._account_sub = (account + "-" + sub).encode()
            self.world.roles.controlled_slot = int(sub)
            self._timestamp = ts
            self._cipher_key = crypto.handshake_key(sk, ts)
            self._field_key = (account + "-" + sub).encode() + crypto.personal_key(sk)
            self._offset = function_offset(sk, ts)
            packet = build_login_packet(sk, ts, account, sub, password_md5, server_index=server_index)
            self._set_deadline(deadline)
            self.sock.sendall(packet)
            status, = decode_string_reply(self._receive(deadline), self._cipher_key,
                                         self._field_key, self._offset, 72, 1)
            if status != b"ok":
                raise ProtocolError("伺服器拒絕登入，已停止；不自動重試")
            self.authenticated = True
            return self
        except BaseException:
            self.close()
            raise

    def request_characters(self) -> CharacterList:
        """傳送 func 79（只有值為 0 的校驗欄位），檢查 func 80 的清單回應。"""
        if not self.authenticated or self.sock is None:
            raise ProtocolError("尚未通過登入驗證")
        deadline = time.monotonic() + self.timeout
        try:
            field = encode_integer(0, self._field_key)
            token = f"*&;{79 + self._offset};{field};#;".encode()
            self._set_deadline(deadline)
            self.sock.sendall(crypto.encrypt_packet(token, self._cipher_key, "C"))
            status, roster = decode_string_reply(self._receive(deadline), self._cipher_key,
                                                self._field_key, self._offset, 80, 2)
            if status != b"successful":
                raise ProtocolError("伺服器未接受角色清單請求，已停止")
            listing = CharacterList(status, roster)
            if roster:
                self.character = parse_character(roster)
                self._cipher_key, self._offset = character_key(self.character, self._timestamp, self._account_sub)
            return listing
        except BaseException:
            self.close()
            raise

    def select_character(self):
        """傳送 func 77 名字及長度校驗；只有 func 78 成功回應才接受選角。"""
        if not self.authenticated or self.sock is None or self.character is None:
            raise ProtocolError("尚未取得可選取的角色")
        deadline = time.monotonic() + self.timeout
        try:
            name = self.character.name
            encoded = crypto.field_encode_shl(name, self._field_key)
            checksum = encode_integer(len(name), self._field_key)
            token = f"*&;{77 + self._offset};{encoded};{checksum};#;".encode()
            self._set_deadline(deadline)
            self.sock.sendall(crypto.encrypt_packet(token, self._cipher_key, "C"))
            from .protocol import decode_event
            deferred = []
            for _ in range(64):
                payload = self._receive(deadline)
                event = decode_event(payload, self._cipher_key, self._field_key, self._offset)
                if event.func == 78:
                    status, info = event.values
                    break
                # 完整登入會先推送設定，再回選角確認；先校驗並保留原順序。
                # 未知封包、伺服器錯誤或驗證題仍在 decode_event 立即停止。
                deferred.append(payload)
            else:
                raise ProtocolError("選角確認前的推送超過上限，已停止")
            if status != b"successful":
                raise ProtocolError("伺服器未接受角色登入，已停止")
            self._frames.extendleft(reversed(deferred))
            # 這一步只證明選角成功；須另收地圖與狀態資料才可宣告進入世界。
            self.selected = True
            return info
        except BaseException:
            self.close()
            raise

    def read_initial_world(self) -> WorldRegion:
        """有上限地接收初始狀態，直到取得校驗正確的地圖區塊，才確認進世界。"""
        if not self.selected or not self.authenticated or self.character is None or self.sock is None:
            raise ProtocolError("尚未完成選角前置步驟")
        deadline = time.monotonic() + self.timeout
        # 初始同步佈局已由實機擷取逐包驗證；仍不接受未知類型。
        try:
            for _ in range(64):
                event = self._read_event(deadline)
                if event.func == 39:
                    floor, x0, y0, x1, y1, _ = event.values
                    if not (0 < floor < 1000000 and 0 <= x0 < x1 <= 8192 and 0 <= y0 < y1 <= 8192):
                        raise ProtocolError("地圖區塊數值異常，已停止")
                    self.entered = True
                    self._next_heartbeat = time.monotonic() + HEARTBEAT_FIRST_SECONDS
                    return WorldRegion(floor, x0, y0, x1, y1)
            raise ProtocolError("初始狀態超過封包數上限，已停止")
        except BaseException:
            self.close()
            raise

    def _read_event(self, deadline):
        from .protocol import decode_event
        event = decode_event(self._receive(deadline), self._cipher_key, self._field_key, self._offset)
        self._last_event_func = event.func
        self.world.apply(event)
        if event.func == 15:
            self.battle.apply(event.values[0])
        elif event.func == 12 and self.battle.combatants:
            self.battle.result_received = True
        elif event.func == 101 and event.values == (0,) and self.battle.result_received:
            if self.battle.active:
                self._battle_ends += 1
            self.battle.active = False
        elif event.func == 7:
            if event.values[0] == 0:
                if self.battle.active:
                    self._battle_ends += 1
                self.battle.active = False
            elif not self.battle.active:
                from .battle import BattleState
                self.battle = BattleState()
        return event

    def _send_command(self, func: int, values: tuple, deadline):
        from .protocol import build_command
        if not self.entered or self.sock is None:
            raise ProtocolError("尚未進入世界，禁止送出遊戲命令")
        if self._command_count >= 10000:
            raise ProtocolError("本次連線命令數已達上限，已停止")
        self._heartbeat_if_due()
        is_control = func == 2025 and values != (b"L|",) and not values[0].startswith((b"ZD|", b"DD|"))
        if is_control and self._last_control_at is not None:
            delay = 1.1 - (time.monotonic() - self._last_control_at)
            if delay > 0:
                time.sleep(delay)
        self._set_deadline(deadline)
        self.sock.sendall(build_command(func, values, self._field_key, self._cipher_key, self._offset))
        self._last_command_func = func
        if is_control:
            self._last_control_at = time.monotonic()
        self._command_count += 1

    def _heartbeat_if_due(self):
        """原客戶端進世界後約每 30 秒送一次 C822，伺服器回 S823。

        兩欄是同一個 4 位元組值的標準 base64，不經欄位金鑰編碼、沒有另外的校驗欄。
        所有本機擷取（兩天、四十多條連線，共 514 筆）的值都是同一個常數 0x0000700D。"""
        if self._next_heartbeat is None or self.sock is None or time.monotonic() < self._next_heartbeat:
            return
        value = base64.b64encode(HEARTBEAT_VALUE).decode("ascii")
        token = f"*&;{822 + self._offset};{value};{value};#;".encode("ascii")
        self.sock.sendall(crypto.encrypt_packet(token, self._cipher_key, "C"))
        self._next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

    def _await(self, predicate, deadline):
        for _ in range(256):
            event = self._read_event(deadline)
            if predicate(event):
                return event
        raise ProtocolError("等待確認的封包數超過上限，已停止")

    def sync(self):
        """排空已收到的推送；回音不代表伺服器非同步操作已完成。"""
        deadline = time.monotonic() + self.timeout
        self._barrier_id += 1
        marker = f"fusion-sync-{self._barrier_id}".encode()
        self._send_command(87, (marker,), deadline)
        return self._await(lambda event: event.func == 88 and event.values == (marker,), deadline)

    def request_roles(self):
        """同步後只查一次 L 清單；全程處理並驗證其他推送，不吞掉錯誤。"""
        try:
            self._phase = "查詢隊伍"
            self.sync()
            deadline = time.monotonic() + self.timeout
            first = not self._controls_initialized
            # 原生客戶端首次附能力版本，伺服器才會恢復既有戰鬥推送。
            self._send_command(2025, (b"L|20261108|" if first else b"L|",), deadline)
            self._await(lambda event: event.func == 2024 and event.values[0].startswith(b"L|"), deadline)
            if first:
                # 原生 0x878e50 固定送 C259(110)；只送名冊版本仍不會
                # 恢復重登入時的戰鬥資料，須完成這個載入通知再同步。
                self._send_command(259, (110,), time.monotonic() + self.timeout)
                self.sync()
                self._controls_initialized = True
            return tuple(self.world.roles.roles[key] for key in sorted(self.world.roles.roles))
        except BaseException:
            self.close()
            raise

    def ensure_party(self, *, leader=1, members=(2, 3, 4, 5)):
        """依新鮮狀態逐人召喚與組隊，已跟隨的隊員完全不重送 T。"""
        from .party import plan_party
        try:
            roles = self.request_roles()
            if self.battle.active:
                if not self.world.roles.ready(leader, members):
                    raise ProtocolError("戰鬥中隊伍不完整，禁止召喚或重新組隊")
                return tuple(self.world.roles.roles[slot] for slot in (leader,) + members)
            plan_party(roles, leader=leader, members=members)
            offline = [slot for slot in members if self.world.roles.roles[slot].status == 0]
            if offline:
                # AL 已實機確認只使離線子號登入，不強制切換目前操作角色。
                deadline = time.monotonic() + self.timeout
                self._send_command(2025, (b"AL",), deadline)
                self._await(lambda _: all(self.world.roles.roles[slot].status != 0 for slot in offline), deadline)
                roles = self.request_roles()
            steps = plan_party(roles, leader=leader, members=members)
            for step in steps:
                if step.action == "登入":
                    raise ProtocolError("一鍵登入後仍有離線隊員，已停止")
                role = self.world.roles.roles[step.slot]
                if role.status == 2:
                    continue
                deadline = time.monotonic() + self.timeout
                tag = step.command[:1]
                self._send_command(2025, (step.command,), deadline)
                self._await(lambda event: event.func == 2024 and (
                    self.world.roles.last_reply == (tag, step.slot)
                    or (tag == b"T" and self.world.roles.last_reply == (b"A", step.slot)
                        and self.world.roles.roles[step.slot].status == 2)), deadline)
                if tag == b"T" and self.world.roles.roles[step.slot].status != 2:
                    raise ProtocolError("隊員回覆為離隊狀態，已停止")
            self.request_roles()
            if not self.world.roles.ready(leader, members):
                raise ProtocolError("組隊後的完整狀態未符合指定隊伍，已停止")
            return tuple(self.world.roles.roles[slot] for slot in (leader,) + members)
        except BaseException:
            self.close()
            raise

    def poll_event(self, wait=0.25):
        """閒置監看不發重試；已開始收到的半包仍受固定期限保護。"""
        if not self.entered or self.sock is None or not 0 <= wait <= 1:
            raise ProtocolError("監看狀態或等待參數錯誤")
        try:
            if not self._frames:
                ready, _, _ = select.select([self.sock], [], [], wait)
                if not ready:
                    return None
            return self._read_event(time.monotonic() + self.timeout)
        except BaseException:
            self.close()
            raise

    def ensure_pets(self, *, mount=None, battle_pet=None, slots=(1, 2, 3, 4, 5)):
        """直接指定已擁有寵物，逐項等待騎乘與出戰回應；不購買、不丟寵、不循環猜狀態。"""
        from .training import allowed_pets, mount_optional, pick_pet, pet_name, preferred_pets, raise_loyalty
        try:
            if not hasattr(self, "_ride_refused"):
                self._ride_refused = {}
            default_mounts, default_fighters = allowed_pets()
            mount_names = (mount,) if mount is not None else default_mounts
            fighter_names = (battle_pet,) if battle_pet is not None else default_fighters
            self._phase = "確認指定寵物"
            self.sync()
            self._pets_verified = not self.battle.active      # 戰鬥中不能換寵物：這次沒有真的檢查
            if self.battle.active:
                self.battle.project_party(self.world)
                return tuple((slot, self.world.actor(slot).mount_pet_index,
                              self.world.actor(slot).battle_pet_index) for slot in slots)
            preferred = preferred_pets() if mount is None and battle_pet is None else None

            def choose(actor, names_for_mount, names_for_fighter):
                mounts = pick_pet(actor.pets.values(), names_for_mount)
                fighters = pick_pet(actor.pets.values(), names_for_fighter)
                if len(mounts) != 1 or len(fighters) != 1 or mounts[0] == fighters[0]:
                    return None
                if any(actor.pets[index].hp <= 0 or actor.pets[index].max_hp <= 0 for index in mounts + fighters):
                    return None
                return mounts[0], fighters[0]

            def fallback(slot):
                actor = self.world.actor(slot)
                if preferred is not None and raise_loyalty():
                    # 優先騎寵忠誠不夠還不能騎：先騎備用騎寵、讓牠出戰練等級（2026-11-20 實測雷龍出戰升級，
                    # 忠誠每級約 +6：1 級 22、4 級 39）。忠誠滿了再換回優先組合。
                    found = choose(actor, mount_names, preferred[0])
                    if found is not None:
                        return found
                found = choose(actor, mount_names, fighter_names)
                if found is None:
                    raise ProtocolError(f"角色 {slot} 的指定寵物不存在、不能唯一確認或尚未恢復，已停止")
                return found

            targets = []
            for slot in slots:
                actor = self.world.actor(slot)
                first = None
                refused_at = self._ride_refused.get(slot)
                if preferred is not None and (refused_at is None or time.monotonic() - refused_at >= RIDE_RETRY_SECONDS):
                    first = choose(actor, *preferred)
                targets.append((slot, first is not None) + (first or fallback(slot)))
            done = []
            for slot, is_preferred, mount_index, fighter_index in targets:
                actor = self.world.actor(slot)
                # 子號登入未推送目前戰寵索引；先明確解除並等確認，
                # 避免把已出戰寵物再次設為待機而被伺服器拒絕。
                if actor.battle_pet_index is None:
                    self._set_battle_pet(slot, -1)
                if actor.mount_pet_index != mount_index and not self._ride(
                        slot, mount_index, give_up_after=RIDE_TRY_SECONDS if is_preferred else None):
                    if not is_preferred:
                        raise ProtocolError(f"角色 {slot} 騎乘被拒絕（忠誠不足），已停止")
                    # 優先的騎寵還不能騎（忠誠不到 100，或等級比人物高超過 5 級）：這次先用備用組合，過一陣子再試
                    self._ride_refused[slot] = time.monotonic()
                    wanted_fighter = fighter_index
                    try:
                        mount_index, fighter_index = fallback(slot)
                        riding = actor.mount_pet_index == mount_index or self._ride(
                            slot, mount_index, give_up_after=RIDE_TRY_SECONDS if mount_optional() else None)
                    except ProtocolError:
                        if not mount_optional() or self.sock is None:
                            raise
                        riding = False
                    if not riding:
                        if not mount_optional():
                            raise ProtocolError(f"角色 {slot} 騎乘被拒絕（忠誠不足），已停止")
                        # 備用的也騎不上：先不騎，只放原本想要的戰寵（2026-11-21 轉生後 1 級，沒有一隻騎得上）
                        mount_index, fighter_index = None, wanted_fighter
                if actor.battle_pet_index != fighter_index:
                    self._set_pet_work(slot, fighter_index, 1)
                    self._set_battle_pet(slot, fighter_index)
                if (mount_index is not None and actor.mount_pet_index != mount_index) or actor.battle_pet_index != fighter_index:
                    raise ProtocolError("指定騎寵或戰寵狀態未確認，已停止")
                done.append((slot, mount_index, fighter_index))
            return tuple(done)
        except BaseException:
            self.close()
            raise

    def _ride(self, slot, mount_index, *, give_up_after=None) -> bool:
        """騎上指定寵物並等伺服器確認；回 False＝伺服器以忠誠不足拒絕（訊息「该骑宠的忠小于100」）。
        替隊員下指令被拒絕時，隊長這條連線收不到那則訊息（2026-11-20 實測），所以試優先騎寵時給 give_up_after：
        這麼多秒沒有騎上就當作被拒絕。沒給的話，沒確認就是錯誤。"""
        actor = self.world.actor(slot)
        if actor.battle_pet_index == mount_index:
            self._set_battle_pet(slot, -1)
        self._set_pet_work(slot, mount_index, 0)

        def send():
            # 實機擷取：目前操作角色自己騎乘走 func 94；func 2012 只用於其他子號，對自己的槽位送 2012 伺服器不回應。
            if slot == self.world.roles.controlled_slot:
                self._send_command(94, (f"R|P|{mount_index}".encode(),), time.monotonic() + self.timeout)
            else:
                self._send_command(2012, (slot, f"R|P|{mount_index}".encode()), time.monotonic() + self.timeout)

        if actor.mount_pet_index not in (None, -1, mount_index):
            # 已經騎著別隻時，騎乘指令只會讓人先下來（騎寵變 -1），要再下一次才會騎上新的那隻
            # （2026-11-20 實測：原本以為是忠誠不足或出戰狀態的關係，其實是少了這一步）。
            send()
            end = time.monotonic() + self.timeout
            while time.monotonic() < end and actor.mount_pet_index != -1:
                self.poll_event(0.25)
            if actor.mount_pet_index != -1:
                raise ProtocolError(f"角色 {slot} 換騎寵前下不來，已停止")
        deadline = time.monotonic() + (give_up_after or self.timeout)
        send()
        refusal = "忠".encode("gbk")
        while time.monotonic() < deadline:
            event = self.poll_event(0.25)
            if actor.mount_pet_index == mount_index:
                return True
            if event is not None and event.func in (36, 263) and any(
                    isinstance(value, bytes) and refusal in value for value in event.values):
                # 拒絕會連著來兩筆（263 與 36 各一筆）：先收完，免得下一次騎乘把剩下那筆當成自己的拒絕
                end = time.monotonic() + 0.8
                while time.monotonic() < end:
                    self.poll_event(0.2)
                return False
        if give_up_after:
            return False
        raise ProtocolError(f"角色 {slot} 的騎乘指令沒有收到確認，已停止")

    def _set_pet_work(self, slot, index, state):
        actor = self.world.actor(slot)
        if actor.pets[index].work_state == state:
            return
        deadline = time.monotonic() + self.timeout
        if slot == self.world.roles.controlled_slot:
            # 實測：目前操作角色自己的寵物待機走 C96(索引, 狀態)，回應為 S107；
            # 對自己的槽位送 2013 伺服器不回應。
            self._send_command(96, (index, state), deadline)
            self._await(lambda event: event.func == 107 and event.values == (index, state), deadline)
            actor.pets[index].work_state = state
            return
        self._send_command(2013, (slot, index, state), deadline)
        self._await(lambda event: event.func == 2018 and event.values == (slot, index, state), deadline)

    def request_destinations(self, category=1):
        """只讀取傳送 NPC 已確認的練級／村莊清單，不執行傳送或購買。"""
        from .travel import parse_destinations
        try:
            if category not in (1, 2, 20, 21, 22, 24, 25):
                raise ValueError("只支援已確認的傳送分類")
            self.sync()
            if self.world.x is None or self.world.y is None:
                raise ProtocolError("尚未確認目前座標，禁止開啟傳送清單")
            # 傳送視窗的物件編號會變（實測過 12128、12126），每次都以伺服器剛開的視窗為準。
            deadline = time.monotonic() + self.timeout
            self._send_command(821, (8, b""), deadline)
            window = self._await(lambda event: event.func == 66 and event.values[0] == 1016, deadline)
            objindex = window.values[3]
            self._send_command(67, (self.world.x, self.world.y, 0, objindex, 0,
                                    f"H|{category}|".encode()), deadline)
            event = self._await(lambda event: event.func == 808 and event.values[0] == 1016
                                and event.values[3] == objindex, deadline)
            return parse_destinations(event.values[4], category)
        except BaseException:
            self.close()
            raise

    def teleport(self, code: int):
        """貼心傳送：開傳送視窗後送目的地代號，只送一次並等伺服器換圖確認。

        客戶端腳本（lua/win/1016、lua/warpanim）：menuSend(8, "") 開視窗（S66，視窗類型 1016），
        選定後播約 1 秒動畫再 menuSend(9, 代號)。2026-11-17 實測：在村長家可傳送、整隊跟隨；在野外不會換圖。"""
        try:
            if not 1 <= int(code) <= 1000000:
                raise ValueError("傳送代號超出範圍")
            self._phase = "貼心傳送"
            self.sync()
            if self.battle.active:
                raise ProtocolError("戰鬥中不可傳送，已停止")
            origin = self.world.map_id
            deadline = time.monotonic() + self.timeout
            self._send_command(821, (8, b""), deadline)
            self._await(lambda event: event.func == 66 and event.values[0] == 1016, deadline)
            time.sleep(1.2)
            deadline = time.monotonic() + self.timeout
            self._send_command(821, (9, str(int(code)).encode("ascii")), deadline)
            self._await(lambda event: event.func == 46 and event.values[0].startswith(b"C")
                        and event.values[0][1:2].isdigit(), deadline)
            self.sync()
            if self.world.map_id == origin:
                raise ProtocolError("傳送後地圖沒有改變，已停止")
            return self.world.map_id, self.world.x, self.world.y
        except BaseException:
            self.close()
            raise

    def return_to_record(self):
        """對應原客戶端「回記錄點」確認鍵，只送一次並等待實際換圖。"""
        try:
            self.sync()
            deadline = time.monotonic() + self.timeout
            self._send_command(81, (1,), deadline)
            self._await(lambda event: event.func == 46 and event.values[0].startswith(b"C")
                        and event.values[0][1:2].isdigit(), deadline)
            return self.world.map_id, self.world.x, self.world.y
        except BaseException:
            self.close()
            raise

    def leave_sam_hospital(self):
        """重現擷取已證實的醫院出口路線；終點必須由伺服器換圖確認。"""
        try:
            self.sync()
            if self.world.map_id != 1006 or (self.world.x, self.world.y) not in ((15, 22), (10, 20)):
                raise ProtocolError("目前不是已驗證的薩村醫院路線起點，已停止")
            route = ((15, 22, b"hh"), (13, 20, b"gg"), (11, 20, b"g")) if self.world.x == 15 else ()
            for x, y, path in route:
                deadline = time.monotonic() + self.timeout
                self._send_command(1, (x, y, path), deadline)
                self.sync()
                time.sleep(0.6)
            deadline = time.monotonic() + self.timeout
            # func 3 是站上事件格後的進入指令；只送移動不會自動換圖。
            self._send_command(3, (3, 0, 10, 20, -1), deadline)
            self._await(lambda event: event.func == 46 and event.values[0].startswith(b"C")
                        and event.values[0][1:2].isdigit(), deadline)
            if self.world.map_id != 1000 or self.world.x != 98 or self.world.y not in (44, 45):
                raise ProtocolError("醫院出口實際換圖結果不符，已停止")
            return self.world.map_id, self.world.x, self.world.y
        except BaseException:
            self.close()
            raise

    def leave_sam_village(self, map_path):
        """只在安全村莊內移動至村口；每段最多兩步，換圖以伺服器資料確認。"""
        from pathlib import Path
        from .navigation import MapGrid
        try:
            self.sync()
            if self.world.map_id != 1000 or self.world.x is None or self.world.y is None:
                raise ProtocolError("目前不在薩姆吉爾村，禁止使用村口路線")
            grid = MapGrid.load(Path(map_path))
            if (grid.width, grid.height) != (self.world.width, self.world.height):
                raise ProtocolError("本機村莊地圖尺寸與伺服器不符，已停止")
            route = grid.path((self.world.x, self.world.y), (49, 118))
            for x, y, path in route:
                deadline = time.monotonic() + self.timeout
                self._send_command(1, (x, y, path), deadline)
                self.sync()
                time.sleep(0.6)
            deadline = time.monotonic() + self.timeout
            self._send_command(3, (3, 0, 49, 118, -1), deadline)
            self._await(lambda event: event.func == 46 and event.values[0].startswith(b"C")
                        and event.values[0][1:2].isdigit(), deadline)
            if self.world.map_id != 100:
                raise ProtocolError("村口實際目的地不符合北島，已停止")
            return self.world.map_id, self.world.x, self.world.y
        except BaseException:
            self.close()
            raise

    def walk_to(self, map_path, goal, *, maximum_steps=40):
        """在目前地圖走到 goal（本機地圖確認可通行；每步等伺服器同步）。中途進入戰鬥就停下並回傳目前座標。"""
        from pathlib import Path
        from .navigation import MapGrid
        try:
            self.sync()
            if self.world.x is None or self.world.y is None:
                raise ProtocolError("目前座標未知，禁止移動")
            grid = MapGrid.load(Path(map_path))
            if (grid.width, grid.height) != (self.world.width, self.world.height):
                raise ProtocolError("本機地圖尺寸與伺服器不符，已停止")
            origin = self.world.map_id
            route = grid.path((self.world.x, self.world.y), tuple(goal), maximum_steps=maximum_steps)
            for x, y, path in route:
                if self.battle.active or self.world.map_id != origin:
                    break
                self._send_command(1, (x, y, path), time.monotonic() + self.timeout)
                self.sync()
                time.sleep(0.6)
            else:
                # 走路時伺服器不回推座標（實測重新登入後位置正確），整條路線送完就以終點為準。
                # 踩到會自動換圖的格子時伺服器已給新座標，不可覆蓋。
                # 座標改由 S41 裡自己的那一筆追蹤；最後一小步的回報可能晚到，差一格以內視為已到。
                # 差更多代表途中被擋住，保留實際座標讓呼叫端判斷。
                if self.world.map_id == origin and not self.battle.active:
                    if max(abs(self.world.x - goal[0]), abs(self.world.y - goal[1])) <= 1:
                        self.world.x, self.world.y = tuple(goal)
            return self.world.map_id, self.world.x, self.world.y
        except BaseException:
            self.close()
            raise

    def enter_event(self):
        """站在換圖事件格上時送進入指令（func 3），等伺服器確認換圖；只送移動不會自動換圖。"""
        try:
            origin = self.world.map_id
            deadline = time.monotonic() + self.timeout
            self._send_command(3, (3, 0, self.world.x, self.world.y, -1), deadline)
            self._await(lambda event: event.func == 46 and event.values[0].startswith(b"C")
                        and event.values[0][1:2].isdigit(), deadline)
            self.sync()
            if self.world.map_id == origin:
                raise ProtocolError("進入事件格後地圖沒有改變，已停止")
            return self.world.map_id, self.world.x, self.world.y
        except BaseException:
            self.close()
            raise

    def drop_item(self, slot: int):
        """把背包一格的道具丟到地上（C19，原版石器的 DI）；等伺服器回報該格清空才算成功。只丟背包格。"""
        try:
            if not 9 <= slot <= 23 or self.world.items is None or slot not in self.world.items:
                raise ProtocolError("要丟的背包格不存在，已停止")
            if self.battle.active:
                raise ProtocolError("戰鬥中不可丟道具，已停止")
            self.sync()
            deadline = time.monotonic() + self.timeout
            self._send_command(19, (self.world.x, self.world.y, slot), deadline)
            self._await(lambda event: event.func == 22 and event.values[0].split(b"|")[:2] == [str(slot).encode(), b""],
                        deadline)
        except BaseException:
            self.close()
            raise

    def talk(self, direction: int, *, maximum_windows=12, wait=5.0):
        """對面前的 NPC 說話（C34），依視窗按鈕回覆（C67）直到沒有新視窗；回傳看過的視窗清單。

        2026-11-18 實測（送貝殼任務）：S66 (0, 按鈕, seqno, objindex, 內文, NPC 名, 圖號)，
        按鈕 12＝是／否、1＝確定；回 C67 (x, y, seqno, objindex, 選擇, "")，選擇值就是按鈕位元（是 4、確定 1）。
        只處理一般訊息視窗（類型 0）；其他類型或看不懂的按鈕一律停下，不亂猜。"""
        from .quest import choose_button
        try:
            if not 0 <= direction <= 7 or not 1 <= maximum_windows <= 30:
                raise ValueError("對話參數錯誤")
            if self.battle.active:
                raise ProtocolError("戰鬥中不可對話，已停止")
            self._phase = "NPC 對話"
            self.sync()
            self._send_command(34, (direction,), time.monotonic() + self.timeout)
            windows = []
            while True:
                window = None
                end = time.monotonic() + wait
                while window is None and time.monotonic() < end:
                    event = self.poll_event(0.3)
                    if event is not None and event.func == 66:
                        window = event
                if window is None:
                    return windows
                if len(windows) >= maximum_windows:
                    raise ProtocolError("對話視窗數超過上限，已停止")
                kind, buttons, seqno, objindex, text, title = window.values[:6]
                windows.append({"kind": kind, "buttons": buttons, "title": title.decode("gbk", "replace"),
                                "text": text.decode("gbk", "replace")})
                try:
                    select = choose_button(kind, buttons)
                except ProtocolError as error:
                    # 把看不懂的視窗原文帶出來，才知道下一步要怎麼支援。
                    raise ProtocolError(f"{error}（{windows[-1]['title']}：{windows[-1]['text'][:300]}）") from None
                time.sleep(1.0)
                self._send_command(67, (self.world.x, self.world.y, seqno, objindex, select, b""),
                                   time.monotonic() + self.timeout)
        except BaseException:
            self.close()
            raise

    def _set_battle_pet(self, slot, index):
        deadline = time.monotonic() + self.timeout
        if slot == self.world.roles.controlled_slot:
            self._send_command(54, (index,), deadline)
            self._await(lambda event: event.func == 55 and event.values == (index, 1), deadline)
        else:
            self._send_command(2016, (slot, index), deadline)
            self._await(lambda event: event.func == 2015 and event.values == (slot, index, 1), deadline)

    def test_novice_battle(self, *, maximum_rounds=3, maximum_seconds=120):
        """薩村門口單場、限回合測試；僅接受指定怪物，不常駐或自動重試。"""
        from .training import TrainingPlan, pet_name
        try:
            self._phase = "戰鬥前同步"
            if not 1 <= maximum_rounds <= 30 or not 1 <= maximum_seconds <= 300:
                raise ValueError("首次戰鬥測試上限錯誤")
            self.sync()
            plan = TrainingPlan.load()
            plan.prepare_encounter(self.world)
            rule = plan.stage(self.world.roles)
            # 計畫檔可用 positions 指定允許的遇敵座標；設為 null 表示該地圖任意位置。
            positions = rule.get("positions", [[637, 493], [637, 494]])
            if self.world.map_id != rule["map_id"] or (
                    positions is not None and [self.world.x, self.world.y] not in positions):
                raise ProtocolError("目前不是練功計畫允許的遇敵位置，已停止")
            total_deadline = time.monotonic() + maximum_seconds
            # 原客戶端選單索引 2「原地遇敵」為 C202(17)，
            # 索引 3「取消遇敵」為 C202(18)；不是只送行走封包。
            if not self.battle.active:
                self._phase = "等待原地遇敵視窗"
                deadline = min(total_deadline, time.monotonic() + self.timeout)
                self._send_command(202, (17,), deadline)
                self._await(lambda event: event.func == 15 and event.values[0].startswith(b"BP|"), deadline)
            self._send_command(202, (18,), min(total_deadline, time.monotonic() + self.timeout))
            self._phase = "取消後續遇敵並同步"
            self.sync()
            completed_windows = set()
            def notify(tag):
                # 原生 0x46d250 / 0x46d403：以連線本機描述符作診斷識別，
                # ZD 為回合視窗開始，DD 為動作動畫完成。不是人類驗證題。
                descriptor = self.sock.fileno() if hasattr(self.sock, "fileno") else 0
                self._send_command(2025, (f"{tag}|{descriptor}|".encode(),),
                                   min(total_deadline, time.monotonic() + self.timeout))
            notify("ZD")
            rounds_sent = 0
            for _ in range(maximum_rounds + 1):
                living_enemies = any(actor.hp > 0 for position, actor in self.battle.combatants.items() if position >= 10)
                # 重登入可能停在敵方全滅的結算視窗，不會再收到可出招 BA。
                # 此時應先收尾，不能永遠等下一回合。
                if not self.battle.combatants or (living_enemies and self.battle.awaiting_round and self.battle.round is not None):
                    self._phase = "等待回合可操作通知"
                    self._await(lambda event: event.func == 15 and event.values[0].startswith(b"BA|"),
                                min(total_deadline, time.monotonic() + self.timeout))
                self.battle.project_party(self.world)
                enemies = [actor for position, actor in sorted(self.battle.combatants.items())
                           if position >= 10 and actor.hp > 0]
                if not enemies:
                    if self.battle.window_id not in completed_windows:
                        notify("DD")
                        completed_windows.add(self.battle.window_id)
                    # 實機擷取的順序是 DD → S55／S101／S90／S63／S2 → C8(0)。
                    # 這裡不可先送 C8(0)：提早送會讓結算變成 `-2|0|0…`（沒有經驗），
                    # 收尾確認改在收到戰鬥結束通知後由 _acknowledge_battle_end 送出。
                    if not self.battle.result_received:
                        self._phase = "等待戰鬥結算"
                        self._await(lambda event: event.func == 12,
                                    min(total_deadline, time.monotonic() + self.timeout))
                    if self.battle.active:
                        self._phase = "等待戰鬥結束通知"
                        self._await(lambda event: event.func == 101 and event.values == (0,),
                                    min(total_deadline, time.monotonic() + self.timeout))
                    self._phase = "戰鬥收尾同步"
                    self.sync()
                    if any(actor.hp > 0 for position, actor in self.battle.combatants.items() if position >= 10):
                        raise ProtocolError("戰鬥收尾後意外再次遇敵，已停止")
                    self.battle.active = False
                    self._acknowledge_battle_end(total_deadline)
                    return rounds_sent
                plan.ready_to_fight(self.world)
                if rounds_sent >= maximum_rounds:
                    raise ProtocolError("已達首次戰鬥回合上限，已停止")
                allowed = rule["allowed_monsters"]
                level_limit = rule.get("maximum_enemy_level", 5)
                # allowed_monsters 含 "*" 表示只用等級上限把關，不限怪物名稱。
                if any(("*" not in allowed and pet_name(enemy.name).replace("凯", "凱") not in allowed)
                                      or enemy.level > level_limit for enemy in enemies):
                    names = "、".join(pet_name(enemy.name).replace("凯", "凱") for enemy in enemies)
                    raise ProtocolError(f"遭遇怪物不符合練功計畫設定（{names}），已停止")
                round_number = self.battle.round
                preferred = [enemy for enemy in enemies if pet_name(enemy.name).replace("凯", "凱") in ("凱彼特", "凱比特")]
                target = (preferred or enemies)[0].position
                if self.battle.round is None:
                    # 原生重登入可在首次 BA 前選擇攻擊；只送一次，
                    # 由確認回覆取得真實回合，不假設一定從第零回合開始。
                    self.battle.claim_initial(0)
                    self._phase = "等待首次攻擊取得真實回合"
                    deadline = min(total_deadline, time.monotonic() + self.timeout)
                    self._send_command(14, (f"H|{target:X}".encode(),), deadline)
                    self._await(lambda event: event.func == 15 and event.values[0].startswith(b"BA|")
                                and bool(self.battle.ready_mask & 1), deadline)
                round_number = self.battle.round
                for position in range(10):
                    if self.battle.ready_mask & (1 << position):
                        continue
                    self.battle.claim(position)
                    self._phase = f"等待戰鬥位置 {position} 的行動確認"
                    command = f"H|{target:X}".encode() if position < 5 else f"W|0|{target:X}".encode()
                    deadline = min(total_deadline, time.monotonic() + self.timeout)
                    owner_position = position if position < 5 else position - 5
                    if owner_position == 0:
                        self._send_command(14, (command,), deadline)
                    else:
                        self._send_command(2010, (owner_position, command), deadline)
                    self._await(lambda event: event.func == 15 and event.values[0].startswith(b"BA|")
                                and self.battle.round == round_number
                                and bool(self.battle.ready_mask & (1 << position)), deadline)
                rounds_sent += 1
                self._phase = "等待下一份回合名冊"
                self._await(lambda event: (event.func == 7 and event.values[0] == 0 and not self.battle.active)
                            or (event.func == 15 and event.values[0].startswith(b"BC|")),
                            min(total_deadline, time.monotonic() + self.timeout))
                if not self.battle.active:
                    self.sync()
                    self._acknowledge_battle_end(total_deadline)
                    return rounds_sent
                notify("DD")
                completed_windows.add(self.battle.window_id)
                notify("ZD")
            raise ProtocolError("已達首次戰鬥回合上限，已停止")
        except BaseException:
            self.close()
            raise

    def escape_battle(self, *, maximum_rounds=8):
        """全隊下逃跑指令直到自己的戰鬥結束；打不贏或要撤退時用，不重試超過上限。

        2026-11-17 實測：人物送 `E`（自己走 C14、其他子號走 C2010），寵物送 `W|FF|FF`；
        逃跑成功會收到 S101。每個人各自判定，隊長脫離後其餘隊員通常在同一輪或下一輪脫離。
        回傳實際送出的輪數；戰鬥本來就不在進行中回 0。"""
        try:
            if not 1 <= maximum_rounds <= 20:
                raise ValueError("逃跑輪數上限錯誤")
            self._phase = "逃跑"
            if not self.battle.active:
                return 0
            ended = {"value": False}

            def watch(event):
                if event.func == 101 or (event.func == 7 and event.values[0] == 0):
                    ended["value"] = True
                return ended["value"]

            def wait(seconds, done):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    event = self.poll_event(0.25)
                    if event is not None:
                        watch(event)
                    if done():
                        return True
                return done()

            descriptor = self.sock.fileno() if hasattr(self.sock, "fileno") else 0
            rounds = 0
            for _ in range(maximum_rounds):
                if ended["value"]:
                    break
                self._send_command(2025, (f"ZD|{descriptor}|".encode(),), time.monotonic() + self.timeout)
                wait(6.0, lambda: ended["value"] or not self.battle.awaiting_round)
                if ended["value"]:
                    break
                for position in range(10):
                    unit = self.battle.combatants.get(position)
                    if unit is None or unit.hp <= 0 or self.battle.ready_mask & (1 << position):
                        continue
                    command = b"E" if position < 5 else b"W|FF|FF"
                    owner = position if position < 5 else position - 5
                    # 自己不一定站第一格：隊長先倒下被送回記錄點後，隊員單獨登入時自己在原本的位置
                    # （2026-11-20 三號登入時自己在第 2 格，原本寫死第 0 格所以逃不掉）。
                    if owner == (self.battle.own_position or 0):
                        self._send_command(14, (command,), time.monotonic() + self.timeout)
                    else:
                        self._send_command(2010, (owner, command), time.monotonic() + self.timeout)
                    wait(1.0, lambda: ended["value"] or bool(self.battle.ready_mask & (1 << position)))
                    if ended["value"]:
                        break
                rounds += 1
                if not ended["value"]:
                    self.battle.awaiting_round = True
                    current = self.battle.round
                    wait(12.0, lambda: ended["value"] or (not self.battle.awaiting_round and self.battle.round != current))
                    self._send_command(2025, (f"DD|{descriptor}|".encode(),), time.monotonic() + self.timeout)
            wait(3.0, lambda: ended["value"])
            if not ended["value"]:
                raise ProtocolError("逃跑未成功，已停止")
            self.battle.active = False
            self._acknowledge_battle_end(time.monotonic() + self.timeout)
            return rounds
        except BaseException:
            self.close()
            raise

    def _acknowledge_battle_end(self, total_deadline):
        """戰鬥結束通知之後再送一次 C8(0)。

        實機擷取中原客戶端是在 S101／S90／S63／S2 之後才送 C8(0)；只在結算前送的話，
        同一連線下一次原地遇敵（C202 17）不會觸發，要重新登入才會遇敵。
        2026-11-17 實測補送後同一連線可連續遇敵。"""
        self._phase = "戰鬥結束後收尾確認"
        self._send_command(8, (0,), min(total_deadline, time.monotonic() + self.timeout))
        self.sync()

    def _set_deadline(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("連線步驟逾時，已停止")
        self.sock.settimeout(remaining)

    def diagnostic(self):
        """只回傳操作階段與遊戲數值，明確排除帳號、密碼、金鑰及原始封包。"""
        return {
            "phase": self._phase,
            "last_command": self._last_command_func,
            "last_event": self._last_event_func,
            "map_id": self.world.map_id,
            "position": [self.world.x, self.world.y],
            "roles": [{"slot": role.slot, "status": role.status, "level": role.level}
                      for role in self.world.roles.roles.values()],
            "battle": {"active": self.battle.active, "round": self.battle.round,
                       "awaiting_round": self.battle.awaiting_round, "window": self.battle.window_id,
                       "ready_mask": self.battle.ready_mask, "result_received": self.battle.result_received,
                       "living_enemies": sum(actor.hp > 0 for position, actor in self.battle.combatants.items()
                                             if position >= 10)},
        }

    def _receive(self, deadline):
        # 同一個步驟固定期限；分段收到資料不會延長期限。
        for _ in range(65540):  # 最壞情況：最大框架每次只收到一個 byte。
            if self._frames:
                return self._frames.popleft()
            self._set_deadline(deadline)
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("伺服器關閉連線，已停止")
            for _, payload in self._reader.feed(data):
                self._frames.append(payload)
        raise ProtocolError("收包次數超過上限，已停止")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
        self.authenticated = False
        self.entered = False
        self.selected = False
        self.character = None
        self._account_sub = b""
        self._timestamp = ""
        self._cipher_key = self._field_key = b""
        self._frames.clear()
        self._reader = FrameReader("server")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
