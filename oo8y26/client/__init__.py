"""本遊戲（oo8y26）無畫面 TCP 遊戲客戶端骨架。

這個套件把「連上遊戲伺服器、收 N 問候、送登入握手、收發遊戲封包」的流程
拆成可單元測試的小零件。**唯一還沒完成的是登入握手的加密**
（Blowfish 衍生 cipher，仍在逆向中，見 doc/LSSPROTO.md §5），
它被隔離在 `handshake.encrypt_handshake` 這個 stub，破解後只要把它實作好即可接上。

模組：
- framing   ：線路框架（0xB0/0xA7 送、0x97/0xE5 收，uint16 LE 長度）編解碼。
- greeting  ：解析連上時伺服器送的 44 byte 明文 N 問候，取出 session key。
- handshake ：登入握手加密的插槽（目前是 stub，待 Blowfish cipher 破解後填入）。
- connection：把上面三者串起來的 GameConnection（純 socket）。
"""
from __future__ import annotations

from .framing import CLIENT_HEAD, CLIENT_TAIL, SERVER_HEAD, SERVER_TAIL, FrameReader, encode_frame
from .greeting import parse_greeting
from .handshake import encrypt_handshake
from .connection import GameConnection

__all__ = [
    "encode_frame",
    "FrameReader",
    "CLIENT_HEAD",
    "CLIENT_TAIL",
    "SERVER_HEAD",
    "SERVER_TAIL",
    "parse_greeting",
    "encrypt_handshake",
    "GameConnection",
]
