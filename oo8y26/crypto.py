"""本遊戲 lssproto 封包加解密（已實機 frida + Python 雙向實證）。

封包 cipher = **XXTEA**（exe 0x8a42f0；delta 0x9E3779B9；標準 mx）。
組裝：token `*&;<func+func1><buffer>;#;` → 補 0 到 4 的倍數 → LE u32 words
     → XXTEA 加密（16-byte key）→ 框架 client `b0|len(uint16 LE)|密文|a7`、server `97|len|密文|e5`。
**登入握手金鑰**：key = MD5("N" + session_key(32hex) + timestamp(10)).hexdigest()[:16]（16 ASCII bytes）。
  session_key、timestamp 來自連線時伺服器的 N 問候（44B 明文）。
遊戲內封包每包有自己的 key（亦為某 MD5 的 hex 前段，推導待補）；但只要有 key 就能用本模組加解密。
"""
from __future__ import annotations
import hashlib
import struct

DELTA = 0x9E3779B9
M = 0xFFFFFFFF


def _mx(z, y, s, e, p, key):
    return ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) & M) ^ (((s ^ y) + (key[(p & 3) ^ e] ^ z)) & M)


def xxtea_encrypt_words(v, key):
    v = list(v)
    n = len(v)
    if n < 2:
        return v
    rounds = 6 + 52 // n
    s = 0
    z = v[-1]
    for _ in range(rounds):
        s = (s + DELTA) & M
        e = (s >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            v[p] = (v[p] + _mx(z, y, s, e, p, key)) & M
            z = v[p]
        y = v[0]
        v[-1] = (v[-1] + _mx(z, y, s, e, n - 1, key)) & M
        z = v[-1]
    return v


def xxtea_decrypt_words(v, key):
    v = list(v)
    n = len(v)
    if n < 2:
        return v
    rounds = 6 + 52 // n
    s = (rounds * DELTA) & M
    y = v[0]
    for _ in range(rounds):
        e = (s >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - _mx(z, y, s, e, p, key)) & M
            y = v[p]
        z = v[-1]
        v[0] = (v[0] - _mx(z, y, s, e, 0, key)) & M
        y = v[0]
        s = (s - DELTA) & M
    return v


def _key_words(key16: bytes):
    assert len(key16) == 16
    return list(struct.unpack("<4I", key16))


def xxtea_encrypt(data: bytes, key16: bytes) -> bytes:
    """data 補 0 到 4 的倍數後整段 XXTEA 加密，回同長度密文。"""
    pad = (-len(data)) % 4
    d = data + b"\x00" * pad
    v = list(struct.unpack("<%dI" % (len(d) // 4), d))
    out = xxtea_encrypt_words(v, _key_words(key16))
    return struct.pack("<%dI" % len(out), *out)


def xxtea_decrypt(cipher: bytes, key16: bytes) -> bytes:
    v = list(struct.unpack("<%dI" % (len(cipher) // 4), cipher))
    out = xxtea_decrypt_words(v, _key_words(key16))
    return struct.pack("<%dI" % len(out), *out)


def handshake_key(session_key: str, timestamp: str) -> bytes:
    """登入握手 XXTEA 金鑰 = MD5('N'+session_key+timestamp).hexdigest()[:16]（ASCII bytes）。"""
    return hashlib.md5(("N" + session_key + timestamp).encode()).hexdigest()[:16].encode()


def frame(payload: bytes, direction: str = "C") -> bytes:
    magic, tail = (0xB0, 0xA7) if direction == "C" else (0x97, 0xE5)
    return bytes([magic]) + struct.pack("<H", len(payload)) + payload + bytes([tail])


def deframe(buf: bytes):
    """回 [(dir, payload)]；容多框黏一起。"""
    out = []
    i = 0
    while i < len(buf):
        m = buf[i]
        d, end = ("C", 0xA7) if m == 0xB0 else (("S", 0xE5) if m == 0x97 else (None, None))
        if d is None:
            break
        ln = buf[i + 1] | (buf[i + 2] << 8)
        out.append((d, buf[i + 3:i + 3 + ln]))
        i += 3 + ln + 1
    return out


def encrypt_packet(token: bytes, key16: bytes, direction: str = "C") -> bytes:
    """token（已含 *&;func...;#;）→ XXTEA → 框架。"""
    return frame(xxtea_encrypt(token, key16), direction)


def decrypt_packet(wire_payload: bytes, key16: bytes) -> bytes:
    """去框架後的密文 → XXTEA 解密 → token bytes（可能含尾端補 0）。"""
    return xxtea_decrypt(wire_payload, key16)


# ---------------------------------------------------------------------------
# lssproto 欄位編解碼（StoneAge autil.c util_256to64_shr / util_shl_64to256）
# 權威來源：github.com/librae8226/gmsv autil.c（石器時代伺服器原始碼）
# token = *&;<func><buffer>;#;  buffer = ;<field0>;<field1>;...
# 每欄位 = util_256to64_shr(value, DEFAULTTABLE, PersonalKey)：
#   自訂 little-endian base64（3 bytes→4 符號），每符號 +key[j] mod 64，j 隨 key rotate。
# DEFAULTTABLE 與 exe 內字串完全吻合。PersonalKey 為 8-byte per-session 金鑰（衍生待定）。
# ---------------------------------------------------------------------------
DEFAULTTABLE = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz{}"
_TI = {c: i for i, c in enumerate(DEFAULTTABLE)}


def field_encode(value: bytes, key) -> str:
    """util_256to64_shr：value bytes → 編碼欄位字串。key = PersonalKey bytes（取 %64）。"""
    dw = 0
    out = []
    j = 0
    n = len(key)
    for i in range(len(value)):
        dw = ((value[i] & 0xFF) << ((i % 3) * 2)) | dw
        out.append(DEFAULTTABLE[((dw & 0x3F) + key[j % n]) % 64]); j += 1
        dw >>= 6
        if i % 3 == 2:
            out.append(DEFAULTTABLE[((dw & 0x3F) + key[j % n]) % 64]); j += 1
            dw = 0
    if dw:
        out.append(DEFAULTTABLE[(dw + key[j % n]) % 64])
    return "".join(out)


def field_decode(field: str, key) -> bytes:
    """util_shl_64to256：編碼欄位字串 → value bytes。"""
    dw = 0
    out = bytearray()
    j = 0
    n = len(key)
    for i in range(len(field)):
        idx = _TI[field[i]]
        if i % 4:
            dw = (((idx + 64 - key[j % n]) % 64) << ((4 - (i % 4)) * 2)) | dw; j += 1
            out.append(dw & 0xFF); dw >>= 8
        else:
            dw = (idx + 64 - key[j % n]) % 64; j += 1
    if dw:
        out.append(dw & 0xFF)
    return bytes(out)


def parse_token(token: bytes):
    """解析客戶端 *&; 或伺服器 &; token，回傳 (func, 欄位清單)。"""
    # 實機伺服器補齊 4 位元組時，NUL 後可能有未清除的記憶體內容。
    # 只接受最多 3 位元組的補位，且第一個補位必須是 NUL。
    end = token.find(b"\x00")
    if end >= 0:
        if len(token) - end > 3:
            raise ValueError("封包 token 補位過長")
        token = token[:end]
    s = token.decode("latin1")
    prefix = "*&;" if s.startswith("*&;") else "&;" if s.startswith("&;") else None
    if prefix is None or not s.endswith(";#;"):
        raise ValueError("封包 token 起訖標記錯誤")
    body = s[len(prefix):-3]
    parts = body.split(";")
    if not parts[0] or any(c not in "0123456789" for c in parts[0]):
        raise ValueError("封包 func 必須是非負十進位整數")
    return parts[0], parts[1:]


# ---------------------------------------------------------------------------
# oo8y26 實際用 util_256to64_shl（減 key），非 _shr。encode: (v6 + 64 - key[j])%64；
# decode util_shr_64to256: (idx + key[j])%64。key = PersonalKey（下方 Blowfish 衍生）。
# ---------------------------------------------------------------------------
def field_encode_shl(value: bytes, key) -> str:
    dw = 0; out = []; j = 0; n = len(key)
    for i in range(len(value)):
        dw = ((value[i] & 0xFF) << ((i % 3) * 2)) | dw
        out.append(DEFAULTTABLE[((dw & 0x3F) + 64 - key[j % n]) % 64]); j += 1
        dw >>= 6
        if i % 3 == 2:
            out.append(DEFAULTTABLE[((dw & 0x3F) + 64 - key[j % n]) % 64]); j += 1
            dw = 0
    if dw:
        out.append(DEFAULTTABLE[(dw + 64 - key[j % n]) % 64])
    return "".join(out)


def field_decode_shl(field: str, key) -> bytes:
    dw = 0; out = bytearray(); j = 0; n = len(key)
    for i in range(len(field)):
        idx = _TI[field[i]]
        if i % 4:
            dw = (((idx + key[j % n]) % 64) << ((4 - (i % 4)) * 2)) | dw; j += 1
            out.append(dw & 0xFF); dw >>= 8
        else:
            dw = (idx + key[j % n]) % 64; j += 1
    if dw:
        out.append(dw & 0xFF)
    return bytes(out)


# ---------------------------------------------------------------------------
# PersonalKey（欄位編碼金鑰）= Blowfish-decrypt(原始 session key 16 bytes) 的 8-char 字串。
# Blowfish：標準 P/S 初始表、key="9372533A2435AFDFED3F9AABF1642CCE"(ASCII)、key XOR big-endian、
#           區塊 I/O little-endian（遊戲 exe 0x8a0510/0x8a0f30/0x8a01c0 逆向確認）。
# 需要標準 Blowfish 初始表（含於 blowfish_tables.py）。
# ---------------------------------------------------------------------------
_BF_KEY = b"9372533A2435AFDFED3F9AABF1642CCE"
_bf_cache = {}


def _bf_schedule():
    if "PS" in _bf_cache:
        return _bf_cache["PS"]
    try:
        from . import blowfish_tables as bt
    except ImportError:
        import os as _os
        import sys as _sys
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        import blowfish_tables as bt
    P = list(bt.P_INIT); S = [list(x) for x in bt.S_INIT]
    j = 0; kl = len(_BF_KEY)
    for c in range(18):
        w = 0
        for _ in range(4):
            w = ((w << 8) | _BF_KEY[j % kl]) & M; j += 1
        P[c] = (bt.P_INIT[c] ^ w) & M

    def _F(x):
        return ((((S[0][(x >> 24) & 0xFF] + S[1][(x >> 16) & 0xFF]) & M) ^ S[2][(x >> 8) & 0xFF]) + S[3][x & 0xFF]) & M

    def enc(L, R):
        for i in range(16):
            L = (L ^ P[i]) & M; R = (R ^ _F(L)) & M; L, R = R, L
        L, R = R, L; R = (R ^ P[16]) & M; L = (L ^ P[17]) & M; return L, R
    L = R = 0
    for i in range(0, 18, 2):
        L, R = enc(L, R); P[i] = L; P[i + 1] = R
    for si in range(4):
        for i in range(0, 256, 2):
            L, R = enc(L, R); S[si][i] = L; S[si][i + 1] = R
    _bf_cache["PS"] = (P, S)
    return P, S


def personal_key(session_key_hex: str) -> bytes:
    """從 greeting 的 32-hex session key 算欄位編碼金鑰（Blowfish 解密後的 8-char 字串）。"""
    P, S = _bf_schedule()

    def _F(x):
        return ((((S[0][(x >> 24) & 0xFF] + S[1][(x >> 16) & 0xFF]) & M) ^ S[2][(x >> 8) & 0xFF]) + S[3][x & 0xFF]) & M
    data = bytes.fromhex(session_key_hex)
    out = bytearray()
    for o in range(0, len(data), 8):
        L = struct.unpack("<I", data[o:o + 4])[0]; R = struct.unpack("<I", data[o + 4:o + 8])[0]
        for i in range(17, 1, -1):
            L = (L ^ P[i]) & M; R = (R ^ _F(L)) & M; L, R = R, L
        L, R = R, L; R = (R ^ P[1]) & M; L = (L ^ P[0]) & M
        out += struct.pack("<I", L) + struct.pack("<I", R)
    z = out.find(b"\x00")
    return bytes(out[:z if z >= 0 else len(out)])


if __name__ == "__main__":
    # lssproto 欄位 codec round-trip（key 範例為某 session 的 PersonalKey）
    K = [12, 43, 51, 62, 30, 53, 55, 24]
    assert field_decode(field_encode(b"win", K), K) == b"win"
    assert field_encode(b"win", K) == "3GPP"
    print("field codec round-trip OK:", field_encode(b"win", K))
    # PersonalKey（Blowfish 衍生）+ _shl 欄位編碼端到端
    pk = personal_key("093267764444A960BD0B42FA6614EBE9")
    assert pk == b"tUMBbKIh", pk
    assert field_decode_shl(field_encode_shl(b"win", pk), pk) == b"win"
    print("personal_key + _shl codec OK:", pk.decode())
    # 自我驗證：用實機抓到的握手資料
    sk, ts = "F804041177568B1BB02694472B15CD00", "1794834799"
    k = handshake_key(sk, ts)
    assert k == b"d12b7db23606be38", k
    print("handshake_key OK:", k.decode())
    # XXTEA round-trip
    tok = b"*&;176;hello-world-token-test;#;"
    c = xxtea_encrypt(tok, k)
    assert xxtea_decrypt(c, k)[:len(tok)] == tok
    print("xxtea round-trip OK")
