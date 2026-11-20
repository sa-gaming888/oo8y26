"""新角色開局整備：穿新手裝備、開資源禮包與自選蛋、取得騎乘資格、吃一顆智慧果。

每個子號單獨登入執行（道具使用 func 17 只對目前登入的角色有效）。每一步都要等到伺服器的
確認訊息才繼續；沒等到或內容不符就停止，不重試。已做過的步驟（道具已不在背包）會自動略過。

實測流程（2026-11-17，見 doc/BOOTSTRAP.md）：
    使用裝備              → 訊息「装备<名稱>」
    使用资源礼包          → 訊息「获得物品:…」（5 顆双倍智慧果）
    使用自选蛋／自选包    → 視窗 func 66（物件 12280／12302）列出選項 → 回覆 func 67 選項編號 → 訊息「恭喜你获得【…】」
    使用*地马年骑乘证     → 訊息「…永久骑乘资格」
    使用双倍智慧果        → 訊息「您的学习经验的能力提升了…」

    python -m oo8y26.newbie --config config/query.local.json --sub 2
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .session import ProtocolError, Session

ITEM_FIELDS = 23
ITEM_SLOTS = 54
FIRST_BAG_SLOT = 9
SETTLE_SECONDS = 1.5

EQUIPMENT = ("新手棒子", "新手凯", "新手的帽子")
# 自選道具：名稱 → (選項編號, 選項名稱)。選項名稱用來核對視窗內容，對不上就停。
CHOICES = {
    "自选马年蛋": (1, "葛雷基欧"),
    "自选老虎蛋": (1, "贝鲁卡"),
    "自选人龙蛋": (1, "利则诺顿"),
    "马年骑乘证自选包": (1, "地马年骑乘证"),
}
RIDE_LICENSE = "*地马年骑乘证"
WISDOM_FRUIT = "双倍智慧果(8小时)"


def parse_items(message: bytes) -> dict[int, str]:
    """解析狀態訊息 `I`：54 格 × 23 欄，回傳 {格: 名稱}（只含有道具的格）。"""
    if not message.startswith(b"I"):
        raise ValueError("不是道具狀態訊息")
    parts = message.split(b"|")
    parts[0] = parts[0][1:]
    if len(parts) != ITEM_SLOTS * ITEM_FIELDS:
        raise ValueError("道具狀態訊息欄位數異常")
    return {index // ITEM_FIELDS: parts[index].decode("gbk")
            for index in range(0, len(parts), ITEM_FIELDS) if parts[index]}


def parse_choice_window(text: bytes) -> list[str]:
    """解析自選視窗內容 `標題|說明|數量|≡ 選項 ≡|…`，回傳選項名稱清單。"""
    parts = text.decode("gbk").split("|")
    count = int(parts[2])
    options = [part.strip("≡ ").strip() for part in parts[3:]]
    if not 1 <= count <= 20 or len(options) != count or not all(options):
        raise ValueError("自選視窗內容異常")
    return options


class Outfitter:
    """包住一條已進世界的連線，逐步使用道具並核對伺服器回應。"""

    def __init__(self, session: Session):
        self.session = session
        self.items: dict[int, str] | None = None
        self.messages: list[str] = []
        self.windows: list[tuple] = []
        original = session.world._status

        def watch_status(message: bytes):
            if message[:1] == b"I":
                self.items = parse_items(message)
            return original(message)

        session.world._status = watch_status

    def _pump(self, seconds: float, until=None) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            event = self.session.poll_event(0.25)
            if event is None:
                continue
            if event.func == 36 and isinstance(event.values[1], bytes):
                self.messages.append(event.values[1].decode("gbk", "replace"))
            elif event.func == 66:
                self.windows.append(event.values)
            if until is not None and until():
                return True
        return until is None

    def load(self):
        self.session.sync()
        self._pump(1.0)
        if self.items is None:
            raise ProtocolError("沒有收到道具狀態，已停止")

    def find(self, name: str) -> int | None:
        slots = [slot for slot, item in self.items.items() if item == name and slot >= FIRST_BAG_SLOT]
        return min(slots) if slots else None

    def _use(self, slot: int):
        # 實測：連續操作太快時，伺服器會顯示視窗卻不受理回覆；每次使用前先停一下並收完推送。
        self._pump(SETTLE_SECONDS)
        world = self.session.world
        self.messages.clear()
        self.windows.clear()
        self.session._send_command(17, (world.x, world.y, slot, 0), time.monotonic() + self.session.timeout)

    def use_expecting(self, name: str, keyword: str) -> bool:
        """使用一個道具並等待含關鍵字的訊息；背包沒有這個道具就略過（回 False）。"""
        slot = self.find(name)
        if slot is None:
            return False
        self._use(slot)
        if not self._pump(6.0, lambda: any(keyword in text for text in self.messages)):
            raise ProtocolError(f"使用「{name}」後沒有收到預期的確認，已停止")
        del self.items[slot]
        return True

    def choose(self, name: str, option: int, expected: str) -> bool:
        """使用自選道具，核對視窗選項後回覆；背包沒有就略過。"""
        slot = self.find(name)
        if slot is None:
            return False
        self._use(slot)
        if not self._pump(6.0, lambda: bool(self.windows)):
            raise ProtocolError(f"使用「{name}」後沒有出現選擇視窗，已停止")
        window = self.windows[-1]
        options = parse_choice_window(window[4])
        if not 1 <= option <= len(options) or options[option - 1] != expected:
            raise ProtocolError(f"「{name}」的選項與預期不符，已停止")
        self._pump(SETTLE_SECONDS)
        world = self.session.world
        self.messages.clear()
        self.session._send_command(67, (world.x, world.y, window[2], window[3], 0, str(option).encode()),
                                   time.monotonic() + self.session.timeout)
        if not self._pump(6.0, lambda: any("恭喜你获得" in text and expected in text for text in self.messages)):
            raise ProtocolError(f"選擇「{expected}」後沒有收到取得確認，已停止")
        del self.items[slot]
        return True


def _connect(config: dict, slot: int) -> tuple:
    """回傳 (連線, 整備器)。整備器要在進世界之前掛上，才接得到登入時推送的道具狀態。"""
    session = Session(timeout=10)
    outfitter = Outfitter(session)
    session.authenticate(config["host"], int(config["port"]), config["account"], str(slot),
                         config["subaccounts"][str(slot)]["password_md5"], server_index=int(config["server_index"]))
    session.request_characters()
    session.select_character()
    session.read_initial_world()
    return session, outfitter


def outfit(config: dict, slot: int, *, eat_fruit: bool = True) -> list[str]:
    """對一個子號做完整開局整備，回傳實際做了的步驟。"""
    done = []
    session, outfitter = _connect(config, slot)
    with session:
        outfitter.load()
        for name in EQUIPMENT:
            if outfitter.use_expecting(name, "装备" + name):
                done.append("穿上" + name)
        if outfitter.use_expecting("资源礼包", "获得物品"):
            done.append("開啟资源礼包")
        for name, (option, expected) in CHOICES.items():
            if outfitter.choose(name, option, expected):
                done.append(f"{name}→{expected}")
    time.sleep(2.0)
    # 重新登入取得最新的背包格位，再使用新拿到的道具。
    session, outfitter = _connect(config, slot)
    with session:
        outfitter.load()
        if outfitter.use_expecting(RIDE_LICENSE, "骑乘资格"):
            done.append("取得騎乘資格")
        if eat_fruit and outfitter.use_expecting(WISDOM_FRUIT, "学习经验的能力提升"):
            done.append("吃一顆双倍智慧果")
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sub", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--no-fruit", action="store_true", help="不吃智慧果（已有經驗加成時使用）")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if config.get("test_only") is not True:
            raise ValueError("只接受測試帳號設定")
        steps = outfit(config, args.sub, eat_fruit=not args.no_fruit)
        print(f"子號 {args.sub}：" + ("、".join(steps) if steps else "沒有需要處理的道具"))
        return 0
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        print(f"子號 {args.sub}：整備停止：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
