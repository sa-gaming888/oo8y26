"""本遊戲登入握手組裝（純 Python）。

加密、字串與整數欄位、func、客戶端時間及校驗和，均由靜態分析與實機擷取驗證。
完整流程見 doc/LSSPROTO.md §18–19；線上回應、選角與進世界由 session.py 處理。
"""
from __future__ import annotations
import hashlib
import socket
import struct
import time

from . import crypto

# exe 常數字串表（0x8abf60）
CLIENT_FINGERPRINT = "84516289189c3b6443ab3b2e7ac12bfe"  # field[3]，所有 session 固定
FIXED_HASH = "5416d7cd6ef195a0f7622a9c56b55e85"          # hash2 用
VERSION = "20261114"                                      # field[4]
PLATFORM = "win"                                          # field[2]

def read_greeting(sock: socket.socket, timeout: float = 10.0):
    """讀 N 問候（44B 明文：'N'+32hex session key+10 digit ts+...），回 (session_key, timestamp)。"""
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < 44:
        chunk = sock.recv(64)
        if not chunk:
            raise ConnectionError("連線關閉，未收到 N 問候")
        buf += chunk
        if buf[:1] != b"N":
            raise ValueError(f"非 N 問候開頭：{buf[:8]!r}")
    s = buf[:44].rstrip(b"\x00").decode("latin1")
    return s[1:33], s[33:43]


def hash2(timestamp: str, account: str, sub: str, password_md5: str) -> str:
    return hashlib.md5((timestamp + account + "-" + sub + password_md5 + FIXED_HASH).encode()).hexdigest()


def function_offset(session_key: str, timestamp: str) -> int:
    """0x7f1a26：取握手 MD5 的第 0、14、31 字元，轉十六進位後模 30。"""
    digest = hashlib.md5(("N" + session_key + timestamp).encode("ascii")).hexdigest()
    return int(digest[0] + digest[14] + digest[31], 16) % 30


def encode_integer(value: int, key: bytes) -> str:
    """0x859960：有號整數依 3142 重排、逐位反相，再用加 key 的欄位編碼。"""
    if not -(1 << 31) <= value < (1 << 31):
        raise ValueError("整數超過有號 32 位元範圍")
    raw = struct.pack("<i", value)
    shuffled = bytes(raw[i] ^ 255 for i in (1, 3, 0, 2))
    return crypto.field_encode(shuffled, key)


def decode_integer(encoded: str, key: bytes) -> int:
    """還原整數欄位；尾端零位元組可能被原編碼省略，以正規編碼反查驗證。"""
    raw = crypto.field_decode(encoded, key)
    if len(raw) > 4:
        raise ValueError("整數欄位超過四個位元組")
    raw = raw.ljust(4, b"\x00")
    value = struct.unpack("<i", bytes(raw[i] ^ 255 for i in (2, 0, 3, 1)))[0]
    if encode_integer(value, key) != encoded:
        raise ValueError("整數欄位不是有效的正規編碼")
    return value


def build_login_token(session_key: str, timestamp: str, account: str, sub: str,
                      password_md5: str, *, server_index: int,
                      client_timestamp: int | None = None) -> bytes:
    """從帳密、伺服器索引與客戶端時間組握手，不沿用擷取的動態欄位。"""
    if not -32768 <= server_index <= 32767:
        raise ValueError("伺服器索引超過有號 16 位元範圍")
    if not account.isascii() or not account.isalnum() or not sub.isascii() or not sub.isdigit():
        raise ValueError("測試帳號與子號格式錯誤")
    if len(password_md5) != 32 or any(c not in "0123456789abcdefABCDEF" for c in password_md5):
        raise ValueError("密碼雜湊必須是 32 碼十六進位")
    now = int(time.time()) if client_timestamp is None else client_timestamp
    pk = crypto.personal_key(session_key)
    strings = [(account + "-" + sub).encode("ascii"), password_md5.encode("ascii"),
               PLATFORM.encode(), CLIENT_FINGERPRINT.encode(), VERSION.encode()]
    h2 = hash2(str(now), account, sub, password_md5).encode("ascii")
    checksum = sum(map(len, strings)) + len(h2) + server_index + now
    checksum = ((checksum + (1 << 31)) % (1 << 32)) - (1 << 31)
    encoded = [crypto.field_encode_shl(value, pk) for value in strings]
    encoded += [encode_integer(server_index, pk), encode_integer(now, pk),
                crypto.field_encode_shl(h2, pk), encode_integer(checksum, pk)]
    func = 151 + function_offset(session_key, timestamp)
    return ("*&;" + str(func) + ";" + ";".join(encoded) + ";#;").encode("ascii")


def build_login_packet(session_key: str, timestamp: str, account: str, sub: str,
                       password_md5: str, **kw) -> bytes:
    """組完整 wire 封包（XXTEA 加密 + 框架），可直接 send。"""
    token = build_login_token(session_key, timestamp, account, sub, password_md5, **kw)
    xk = crypto.handshake_key(session_key, timestamp)
    return crypto.encrypt_packet(token + b"\x00", xk, "C")


def login(host: str, port: int, account: str, sub: str, password_md5: str,
          *, server_index: int, timeout: float = 10.0):
    """驗證一次無 GUI 帳號登入，回傳 Session；呼叫端須用 with 或 close 收尾。"""
    from .session import Session
    return Session(timeout=timeout).authenticate(host, port, account, sub, password_md5,
                                                  server_index=server_index)
