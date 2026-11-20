"""登入握手加密的插槽（**尚未破解，這裡是 stub**）。

整個無畫面客戶端唯一缺的一塊就是這個函式。其餘（framing、greeting、connection）都已可用；
只要有人把登入握手的 cipher 逆向完成，實作 `encrypt_handshake` 就能從頭到尾跑通登入。

目前已知（見 doc/LSSPROTO.md §5，frida 動態分析）：

- 登入握手 cipher 是 **Blowfish**（不是 TEA/XTEA/AES，難怪舊的暴力嘗試全失敗）。
  - 16 輪 Feistel 區塊函式 `0x8a0820(ctx,&L,&R)`；F 函式 `0x8a01c0`（4 個 S-box）。
  - 金鑰展開呼叫區塊函式共 **521** 次 =（18 個 P + 1024 個 S）/2 → 標準 Blowfish key schedule。
- 金鑰來源：N 問候裡的 **32 碼十六進位 session key**（見 greeting.parse_greeting）。
  已知 P-array 會跟標準 pi 常數 XOR 出週期 32 的金鑰，但「直接拿去解密對不上」，
  byte-order／實際衍生方式待定 → 預計用 frida 攔資料加密 block 直接取明文收尾。
- 線路框架：200 byte 的登入框架 `b0 | len(LE)=0xC4(196) | payload(196) | a7`。
  payload = **4 byte 前綴 + 192 byte**（192 = 24 個 8-byte Blowfish 區塊）。
- 要加密的明文（記憶體）：登入憑證＝子號 `<手機>-<n>`（例 `<帳號>-1`）＋ 8 碼明文密碼。

破解後怎麼接：
    實作 encrypt_handshake(plaintext, session_key) 回傳那 196 byte 的 payload，
    GameConnection.send_handshake() 會自動把它用 framing.encode_frame("client", ...)
    包成 200 byte 的 b0…a7 框架送出。`plaintext` 傳進來的是「4 byte 前綴 + 192 byte」
    的明文，`session_key` 是 greeting 解出的 32 字原文字串。
"""
from __future__ import annotations

_PENDING_MSG = (
    "blowfish handshake cipher pending — see doc/LSSPROTO.md §5"
)

# 已知的框架常數，破解者實作時可直接引用（避免魔術數字散落）。
HANDSHAKE_FRAME_LEN = 200          # 整個登入框架總長（b0 + len2 + payload + a7）
HANDSHAKE_PAYLOAD_LEN = 196        # 框架內 payload 長度（= 0x00C4）
HANDSHAKE_PREFIX_LEN = 4           # payload 開頭的前綴 byte 數
HANDSHAKE_BODY_LEN = 192           # 前綴之後的 Blowfish 密文長度（24 個 8-byte 區塊）


def encrypt_handshake(plaintext: bytes, session_key: str) -> bytes:
    """把登入握手明文加密成 196 byte 的 payload。**尚未實作**。

    參數：
        plaintext   ：待加密的明文（4 byte 前綴 + 192 byte 內容，見本檔頂註解）。
        session_key ：N 問候解出的 32 碼十六進位 session key 原文字串。
    回傳：
        196 byte 的 payload（之後由 connection 包成 b0…a7 框架）。

    破解完成前一律 raise NotImplementedError。破解後請移除這個 raise、填入實作。
    """
    raise NotImplementedError(_PENDING_MSG)
