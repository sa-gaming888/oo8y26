"""GameConnection：把 framing / greeting / handshake 串成一條遊戲連線（純 socket）。

流程：
    conn = GameConnection()
    greeting = conn.connect("<伺服器位址>", 9001)   # 連上並收 N 問候，回解析結果
    conn.send_handshake(plaintext)                      # 送登入握手（目前會因 cipher 未破解而 raise）
    for magic, payload in conn.recv_frames():           # 收伺服器送來的框架（0x97…0xE5）
        ...
    conn.close()

- 登入握手的加密是插槽（handshake.encrypt_handshake），破解後自動接上，見該檔。
- 收端用 FrameReader 緩衝，正確處理「一個 TCP segment 含多個框架」與「框架被切兩段」。
"""
from __future__ import annotations

import socket
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .framing import FrameReader, encode_frame
from .greeting import parse_greeting
from .handshake import encrypt_handshake as _default_encrypt

# N 問候是裸送明文、不走 framing；實擷取 44 byte，這裡抓大一點一次收完。
_GREETING_BUFSIZE = 256
_RECV_BUFSIZE = 65536


class GameConnection:
    """一條連到本遊戲遊戲伺服器的 TCP 連線。

    encrypt_handshake 可從外部注入（破解完成前方便對拍/測試）；預設用
    oo8y26.client.handshake.encrypt_handshake（目前為 stub）。
    """

    def __init__(
        self,
        encrypt_handshake: Callable[[bytes, str], bytes] = _default_encrypt,
        timeout: float = 15.0,
    ) -> None:
        self._encrypt_handshake = encrypt_handshake
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._reader = FrameReader("server")
        self.greeting: Optional[Dict[str, str]] = None

    # --- 連線 / 問候 ---

    def connect(self, ip: str, port: int) -> Dict[str, str]:
        """連上伺服器，收並解析 N 問候，回傳 parse_greeting 的結果（也存到 self.greeting）。"""
        if self._sock is not None:
            raise RuntimeError("已經連線，請先 close()")
        sock = socket.create_connection((ip, port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        self._sock = sock
        self.greeting = self._recv_greeting()
        return self.greeting

    def _recv_greeting(self) -> Dict[str, str]:
        data = self._sock.recv(_GREETING_BUFSIZE)
        if not data:
            raise ConnectionError("連上後伺服器沒送 N 問候就關了（空服或拒絕，見 doc/LSSPROTO.md §0）")
        return parse_greeting(data)

    @property
    def session_key(self) -> str:
        """已解析的 session key（32 碼十六進位原文）；還沒 connect 就 raise。"""
        if self.greeting is None:
            raise RuntimeError("還沒 connect／還沒收到 N 問候")
        return self.greeting["session_key"]

    # --- 送 ---

    def send_handshake(self, plaintext: bytes) -> bytes:
        """加密登入握手明文、包成框架、送出，回傳實際送出的 bytes。

        明文格式見 handshake.py（4 byte 前綴 + 192 byte）。cipher 未破解前，
        這裡會從 encrypt_handshake stub 冒出 NotImplementedError（這是預期行為）。
        """
        payload = self._encrypt_handshake(plaintext, self.session_key)
        return self.send_frame(payload)

    def send_frame(self, payload: bytes) -> bytes:
        """把 payload 包成 client 框架（0xB0…0xA7）送出，回傳實際送出的 bytes。"""
        frame = encode_frame("client", payload)
        self._require_sock().sendall(frame)
        return frame

    def send_raw(self, data: bytes) -> None:
        """不經框架，直接把 bytes 送出（偵錯用）。"""
        self._require_sock().sendall(data)

    # --- 收 ---

    def recv_frames(self) -> Iterator[Tuple[int, bytes]]:
        """阻塞式產生器：持續 recv，逐一 yield 伺服器送來的 (magic, payload)。

        伺服器關閉連線（recv 回空）時正常結束迴圈。
        """
        sock = self._require_sock()
        while True:
            data = sock.recv(_RECV_BUFSIZE)
            if not data:
                return
            yield from self._reader.feed(data)

    def recv_once(self) -> List[Tuple[int, bytes]]:
        """收一次（一個 recv），回傳這次能湊齊的框架清單（可能是空的）。

        回傳 None 代表伺服器已關閉連線。
        """
        sock = self._require_sock()
        data = sock.recv(_RECV_BUFSIZE)
        if not data:
            return None
        return list(self._reader.feed(data))

    # --- 收尾 ---

    def close(self) -> None:
        """關閉連線（可重複呼叫）。"""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise RuntimeError("還沒 connect")
        return self._sock

    # --- context manager ---

    def __enter__(self) -> "GameConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
