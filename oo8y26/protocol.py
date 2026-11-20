"""有欄位佈局與校驗的遊戲事件解碼；由實機離線重播驗證資料表。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import crypto
from .login import decode_integer, encode_integer
from .session import ProtocolError, decode_fields


@dataclass(frozen=True)
class GameEvent:
    func: int
    schema: str
    values: tuple = field(repr=False)


def infer_schema(fields: list[str], key: bytes) -> set[str]:
    """利用整數／字串長度校驗推導候選佈局，只用於離線分析，不猜測線上命令。"""
    checksum = decode_integer(fields[-1], key)
    possibilities = {0: {""}}
    for encoded in fields[:-1]:
        options = []
        try:
            options.append(("I", decode_integer(encoded, key)))
        except (ValueError, KeyError):
            pass
        try:
            value = crypto.field_decode_shl(encoded, key)
            if crypto.field_encode_shl(value, key) == encoded:
                options.append(("S", len(value)))
        except (ValueError, KeyError):
            pass
        updated = {}
        for total, schemas in possibilities.items():
            for kind, contribution in options:
                new_total = ((total + contribution + (1 << 31)) % (1 << 32)) - (1 << 31)
                bucket = updated.setdefault(new_total, set())
                bucket.update(schema + kind for schema in schemas)
                if len(bucket) > 32:
                    raise ValueError("離線欄位候選過多")
        if len(updated) > 4096:
            raise ValueError("離線欄位組合超過上限")
        possibilities = updated
    return possibilities.get(checksum, set())


@lru_cache(maxsize=1)
def schemas():
    path = Path(__file__).resolve().parents[1] / "data" / "protocol_schema.json"
    return json.loads(path.read_text(encoding="utf-8"))["server"]


def decode_event(payload: bytes, cipher_key: bytes, field_key: bytes, offset: int) -> GameEvent:
    """只接受已離線驗證的佈局；候選都不符合就停止，不吞包或重試。"""
    try:
        if len(payload) < 8 or len(payload) % 4:
            raise ValueError
        token = crypto.xxtea_decrypt(payload, cipher_key)
        if not token.startswith(b"&;"):
            raise ValueError
        func, fields = crypto.parse_token(token)
        base = int(func) - offset
        if base == 7:
            # 原客戶端 0x85d099：前兩欄相加校驗，第三欄就是校驗值；
            # 後兩欄是可選附加資料，不計入校驗，不能錯把最後 -1 當校驗。
            if not 3 <= len(fields) <= 5:
                raise ValueError
            decoded = [decode_integer(value, field_key) for value in fields]
            total = ((decoded[0] + decoded[1] + (1 << 31)) % (1 << 32)) - (1 << 31)
            if decoded[2] != total:
                raise ProtocolError("進入戰鬥回應校驗錯誤，已停止")
            extra = decoded[3:] + [0, -1][len(decoded) - 3:]
            # 一般遇敵是 (1, 場地)；NPC 對話觸發的首領戰數值不同（2026-11-18 五兄弟首領），放寬第一欄與附加欄。
            if not 0 <= decoded[0] <= 16 or not 0 <= decoded[1] < 1000:
                raise ProtocolError(f"進入戰鬥回應包含尚未確認的狀態，已停止（數值 {decoded}）")
            return GameEvent(7, "IIII", (decoded[0], decoded[1], *extra))
        variants = schemas().get(str(base), {}).get("schemas", [])
        if not variants:
            raise ProtocolError(f"尚未支援的遊戲回應 {base}，已停止")
        for schema in variants:
            if len(fields) != len(schema) + 1:
                continue
            try:
                values = decode_fields(payload, cipher_key, field_key, offset, schema, base)
            except ProtocolError:
                continue
            event = GameEvent(base, schema, tuple(values))
            check_event(event)
            return event
        raise ProtocolError(f"遊戲回應 {base} 欄位或校驗錯誤，已停止")
    except (ValueError, AssertionError, KeyError, IndexError):
        raise ProtocolError("遊戲回應解碼失敗，已停止；敏感內容已隱藏") from None


BROADCAST_NOTICES = ("游戏内只能输入英文无法打字",)


def check_event(event: GameEvent):
    """人工驗證與系統錯誤先於任何狀態更新或下一步操作。"""
    for value in event.values:
        if not isinstance(value, bytes):
            continue
        for encoding in ("gbk", "utf-8"):
            text = value.decode(encoding, errors="ignore")
            if any(word in text for word in ("验证码", "驗證碼", "防脚本", "防腳本", "请回答", "請回答")):
                raise ProtocolError("偵測到人工驗證，已停止，請使用者處理")
            system_message = (event.func == 36 and event.values[0] == -1) or (
                event.func == 2026 and event.values[0] == 0 and value.startswith(b"E|"))
            # 伺服器定時廣播的客服公告含「无法打字」，不是操作失敗。
            if system_message and any(notice in text for notice in BROADCAST_NOTICES):
                continue
            if system_message:
                if any(word in text for word in ("无法", "無法", "失败", "失敗", "错误", "錯誤",
                                                 "只有主控状态", "子号请切换登录")):
                    raise ProtocolError(f"伺服器回報操作失敗，已停止；不自動重試（原文：{text[:80]}）")
                if any(word in text for word in ("请过", "請過", "稍后再", "稍後再", "频率太高", "頻率太高", "操作频繁", "操作頻繁")):
                    raise ProtocolError(f"伺服器要求操作冷卻，已停止；不自動重試（原文：{text[:80]}）")


def build_command(func: int, values: tuple, field_key: bytes, cipher_key: bytes, offset: int) -> bytes:
    """已知命令的 I/S 欄位組裝；所有欄位一併計入有號 32 位元校驗和。"""
    encoded = []
    checksum = 0
    for value in values:
        if isinstance(value, int):
            encoded.append(encode_integer(value, field_key))
            checksum += value
        elif isinstance(value, bytes):
            encoded.append(crypto.field_encode_shl(value, field_key))
            checksum += len(value)
        else:
            raise ValueError("命令只接受整數與位元組欄位")
    checksum = ((checksum + (1 << 31)) % (1 << 32)) - (1 << 31)
    encoded.append(encode_integer(checksum, field_key))
    token = (f"*&;{func + offset};" + ";".join(encoded) + ";#;").encode()
    return crypto.encrypt_packet(token, cipher_key, "C")
