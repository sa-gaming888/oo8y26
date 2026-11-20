"""解析伺服器在連上瞬間送來的「N 問候」。

連上遊戲伺服器後，伺服器第一件事是送一段 **明文 ASCII**（不走 framing，直接裸送）：

    N + 32 碼十六進位 session key + 10 位時戳 + (選)1 位子號 + "\\0"

例（實擷取 44 byte）：`N8999AE3089C8298B30A8C75D3600DD261794826012\\0`
            （session key=8999AE3089C8298B30A8C75D3600DD26，時戳=1794826012）

這 32 碼十六進位字串就是後續遊戲封包加密用的 session key（見 oo8y26/lssproto.py），
也是登入握手 Blowfish cipher 的金鑰來源（見 doc/LSSPROTO.md §5）。
"""
from __future__ import annotations

from typing import Dict

_HEXDIGITS = set("0123456789abcdefABCDEF")

SESSION_KEY_LEN = 32
TIMESTAMP_LEN = 10


def parse_greeting(data: bytes) -> Dict[str, str]:
    """解析 N 問候，回傳 {"session_key", "timestamp", "subacct_num"}（皆為 str）。

    - session_key：32 碼十六進位字串（原文字串，不轉成 bytes；lssproto.derive_key2 要的就是這 32 字原文）。
    - timestamp  ：緊接著的 10 位數字。
    - subacct_num：時戳之後、結尾 "\\0" 之前剩下的字（實擷取有時為空字串）。

    開頭不是 'N'、或 session key 不是 32 碼十六進位，就 raise ValueError。
    """
    if not data:
        raise ValueError("空的問候資料")
    # 去掉可能的結尾 NUL（以及多餘尾端空白），再當成 ASCII 文字處理。
    text = data.split(b"\x00", 1)[0].decode("ascii", "strict")
    if not text.startswith("N"):
        raise ValueError(f"問候開頭不是 'N'：{text[:8]!r}")

    body = text[1:]
    session_key = body[:SESSION_KEY_LEN]
    if len(session_key) != SESSION_KEY_LEN or any(c not in _HEXDIGITS for c in session_key):
        raise ValueError(f"session key 不是 32 碼十六進位：{session_key!r}")

    rest = body[SESSION_KEY_LEN:]
    timestamp = rest[:TIMESTAMP_LEN]
    subacct_num = rest[TIMESTAMP_LEN:]
    if len(timestamp) != TIMESTAMP_LEN or not timestamp.isdigit():
        raise ValueError("問候時戳必須是十位數字")
    if subacct_num and (len(subacct_num) != 1 or not subacct_num.isdigit()):
        raise ValueError("問候子號必須是單一數字")

    return {
        "session_key": session_key,
        "timestamp": timestamp,
        "subacct_num": subacct_num,
    }
