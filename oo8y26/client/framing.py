"""線路框架（wire framing）編解碼。

實機封包擷取確認（見 doc/LSSPROTO.md §5）：

    client→server： 0xB0 | len(uint16 LE) | payload(len bytes) | 0xA7      # 全長 = len + 4
    server→client： 0x97 | len(uint16 LE) | payload(len bytes) | 0xE5      # 全長 = len + 4

- 長度欄是 **小端序 uint16**，只算 payload（不含頭尾兩個 magic 與長度欄本身）。
- 一個 TCP segment 可能塞了好幾個相連的框架，所以收端要用緩衝、逐框切出。

這個模組全是純函式／純狀態機，不碰 socket，可直接單元測試。
"""
from __future__ import annotations

from typing import Iterator, List, Tuple

# 方向名稱 → (開頭 magic, 結尾 magic)
CLIENT_HEAD = 0xB0
CLIENT_TAIL = 0xA7
SERVER_HEAD = 0x97
SERVER_TAIL = 0xE5

_FRAMING = {
    "client": (CLIENT_HEAD, CLIENT_TAIL),  # 我方送出（client→server）
    "server": (SERVER_HEAD, SERVER_TAIL),  # 伺服器送來（server→client）
}

_MAX_PAYLOAD = 0xFFFF  # uint16 長度欄上限


def encode_frame(direction: str, payload: bytes) -> bytes:
    """把 payload 包成一個線路框架。

    direction：`"client"`（我方送，0xB0…0xA7）或 `"server"`（0x97…0xE5，通常只拿來測試/對拍）。
    回傳 head(1) + len(2, LE) + payload + tail(1)，共 len(payload)+4 byte。
    """
    try:
        head, tail = _FRAMING[direction]
    except KeyError:
        raise ValueError(f"direction 必須是 'client' 或 'server'，不是 {direction!r}")
    n = len(payload)
    if n > _MAX_PAYLOAD:
        raise ValueError(f"payload 太長：{n} byte（uint16 長度欄上限 {_MAX_PAYLOAD}）")
    return bytes((head, n & 0xFF, (n >> 8) & 0xFF)) + payload + bytes((tail,))


class FrameReader:
    """串流式框架切割器：把陸續 recv 到的 byte 餵進來，吐出一個個完整框架。

    用法：
        reader = FrameReader("server")       # 解伺服器送來的框架
        for magic, payload in reader.feed(chunk):
            ...                              # magic 是開頭那個 byte（0x97）

    對同一方向的 head/tail 已知，所以會驗證開頭與結尾 magic，對不上就 raise
    （代表串流失步，不應該默默吞掉）。沒收滿的半個框架會留在內部緩衝，等下次 feed。
    """

    def __init__(self, direction: str) -> None:
        if direction not in _FRAMING:
            raise ValueError(f"direction 必須是 'client' 或 'server'，不是 {direction!r}")
        self.direction = direction
        self._head, self._tail = _FRAMING[direction]
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[Tuple[int, bytes]]:
        """餵一段剛 recv 到的 byte，yield 這次能湊齊的每個 (magic, payload)。"""
        self._buf += data
        yield from self._drain()

    def _drain(self) -> Iterator[Tuple[int, bytes]]:
        buf = self._buf
        while True:
            if len(buf) < 4:  # 連 head+len(2)+tail 都不夠，等更多資料
                return
            head = buf[0]
            if head != self._head:
                raise ValueError(
                    f"框架開頭 magic 不符：拿到 0x{head:02X}，預期 0x{self._head:02X}"
                )
            n = buf[1] | (buf[2] << 8)  # uint16 LE
            total = n + 4
            if len(buf) < total:  # payload（含結尾 magic）還沒收滿
                return
            tail = buf[total - 1]
            if tail != self._tail:
                raise ValueError(
                    f"框架結尾 magic 不符：拿到 0x{tail:02X}，預期 0x{self._tail:02X}"
                )
            payload = bytes(buf[3:3 + n])
            del buf[:total]
            yield head, payload

    def pending(self) -> int:
        """內部緩衝裡還沒湊成完整框架的 byte 數（偵錯用）。"""
        return len(self._buf)


def iter_frames(direction: str, data: bytes) -> List[Tuple[int, bytes]]:
    """一次性把一整段 byte 切成框架清單（純函式，方便測試）。

    要求 data 剛好是整數個完整框架，否則（有半個框架殘留）會 raise。
    """
    reader = FrameReader(direction)
    frames = list(reader.feed(data))
    if reader.pending():
        raise ValueError(f"尾端殘留 {reader.pending()} byte，不是完整框架")
    return frames
