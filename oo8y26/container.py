"""容器進入點：啟動時帶入帳號密碼，自動登入五隻角色、組隊、練功（使用者 2026-11-20「練功模組要可以打包成 docker」）。

    docker run -d -e GAME_ACCOUNT=… -e GAME_PASSWORD=… -e GAME_HOST=… -e GAME_API=… -e TEST_ACCOUNT=1 \\
        -v data:/data <映像>
    python -m oo8y26.container --check      # 只檢查設定與資料檔（CI 用，不登入）

環境變數：
  GAME_ACCOUNT        帳號（必填，除非用 ACCOUNT_FILE）
  GAME_PASSWORD       密碼（必填；或用 GAME_PASSWORD_FILE 指向 docker secret 檔）
  GAME_HOST           遊戲伺服器位址（必填）
  GAME_API            帳號 API 位址，例 http://<主機>:<埠>（必填；啟動時用它查五個子帳號的登入憑證）
  TEST_ACCOUNT        一定要設 1：確認這是測試帳號（程式只接受測試帳號）
  ACCOUNT_FILE        （選填）改用掛載的帳號設定檔（JSON，已含子帳號憑證）；有設就不用上面的帳密與 GAME_API
  ACTIVITY            活動名稱（預設「練功」；活動設定檔裡類型是練功的才收）
  TARGET_LEVEL        目標等級（選填，蓋過活動設定檔）；全隊到了就停
  TELEPORT            貼心傳送代號（選填，蓋過活動設定檔）
  BATTLES             最多打幾場（預設 6000）
  PROFILES_FILE       活動設定檔路徑（預設：有掛載 /config/module_profiles.json 就用，沒有用映像內建的）
  MAP_DIR             地圖目錄（預設用映像內建的 data/maps）
  API_TOKEN           查詢 API 的 token（至少 16 字元；或 API_TOKEN_FILE）。有設才開 8790 埠
  API_PORT            查詢 API 的埠（預設 8790）
  OPENING             0＝不做新角色開局整備（穿新手裝備、開禮包、選蛋與騎乘證）；預設會做，做過的自動略過
  DISCARD_PETS        開局要丟掉的寵物名稱（遊戲內原文，逗號分隔；預設「凯比」＝出生寵）；none＝不丟。
                      騎寵、戰寵與 10 級以上的不丟；丟寵不可逆
  FAST_BATTLE         0＝分級練功不開快速戰鬥；預設開（只要是會遇敵的地方都開快戰）
  STATUS_SECONDS      每隔幾秒把人物與隊伍狀態印到日誌（預設 60）；0＝不印
  ROUTE_UNTIL         分級路線練到幾級才換成活動設定檔的練功點（預設 123）；1＝不走分級路線
  ROUTE_HOURS         分級練功的時間上限（小時，預設 240）
  HOLD_ON_STOP        1＝停下來之後（出錯、到目標等級）容器留著不結束，也不重新登入；Kubernetes 上一定要設，
                      否則 Deployment 會一直重開、重新登入。留著的期間查詢 API 照常可查

- 密碼只放在行程的記憶體，不寫檔、不進日誌（`describe()` 只列有沒有設）。
- 出錯就停：逾時、錯誤回應、數值異常就記一行「停止：…」並結束（結束碼 1），**不自動重開**；請不要設 restart 策略。
- `docker stop`（SIGTERM）→ 立刻登出並結束（結束碼 0）；打到一半的戰鬥下次啟動時會先打完。
- 日誌只印到標準輸出（`docker logs` 看）。面板快照、活動標記寫在 /data（映像裡 captures 連到 /data）。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping

from . import api, modules, panels
from .navigation import MapGrid
from .protocol import schemas
from .session import ProtocolError

ROOT = Path(__file__).resolve().parent.parent
BUILTIN_MAPS = ROOT / "data" / "maps"
MOUNTED_PROFILES = Path("/config/module_profiles.json")
API_PORT = 8790
MIN_TOKEN = 16
TEAM_SIZE = 5
HTTP_TIMEOUT = 15
GROUP_ID = "1"
ROUTE_UNTIL = 123        # 分級路線練到幾級才換活動設定檔的練功點（2026-11-20 舊隊伍在 123 級換到英雄島 254）
ROUTE_HOURS = 240        # 分級練功的總時間上限（小時）
OPENING_GAP = 2.0
RETRY_TIMES, RETRY_WAIT = 2, 30      # 連線逾時最多重做幾次、每次隔幾秒
STATUS_SECONDS = 60      # 每隔幾秒把人物與隊伍狀態印到日誌
DISCARD_MAX_LEVEL = 10   # 開局只丟這個等級以下的寵物


class Stopped(BaseException):
    """收到停止訊號（docker stop）。繼承 BaseException：不會被練功程式裡的錯誤處理吃掉。"""


_stop = {"requested": False}


def check_stop() -> None:
    """安全的停止點（兩場戰鬥之間、兩隻角色的整備之間、等待中）才真的停：收到停止訊號後在這裡丟出 Stopped。
    2026-11-20：換版時在戰鬥打到一半登出，下一個 Pod 開局就卡在殘留戰鬥逾時。"""
    if _stop["requested"]:
        raise Stopped()


def patient_sleep(seconds: float) -> None:
    """可以被停止訊號打斷的等待。"""
    end = time.monotonic() + seconds
    while True:
        check_stop()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def _secret(env: Mapping[str, str], name: str) -> str:
    """讀 NAME，或 NAME_FILE 指到的檔案內容（docker secret）。"""
    value = env.get(name, "")
    if value:
        return value
    path = env.get(name + "_FILE", "")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"讀不到 {name}_FILE：{error.strerror}") from None


def _number(env: Mapping[str, str], name: str, default=None):
    value = env.get(name, "")
    if not value:
        return default
    if not value.isdigit():
        raise ValueError(f"{name} 要是正整數")
    return int(value)


def _post_json(base: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload, indent="\t", ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base.rstrip("/") + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except ValueError:
        raise ValueError(f"帳號 API {path} 回應不是 JSON，已停止") from None
    except urllib.error.URLError as error:
        raise ValueError(f"帳號 API {path} 連不上：{error.reason}") from None


def fetch_subaccounts(base: str, account: str, password: str, post=_post_json) -> dict:
    """登入帳號 API、列出子帳號，回傳「編號 → 登入憑證」。只讀取，不新增子帳號；內容不對就停。
    錯誤訊息只帶伺服器的狀態碼與訊息，不帶帳密。"""
    result = post(base, "/api/csa-account/sign-in", {"Code": "", "Name": account, "PassWord": password})
    token = result.get("Token")
    if result.get("status") != 200 or not token:
        raise ValueError(f"帳號登入失敗（狀態 {result.get('status')}：{result.get('message')}），已停止")
    result = post(base, "/api/csa-account/account-list", {"GroupId": GROUP_ID, "Name": account, "api_token": token})
    if result.get("status") != 200:
        raise ValueError(f"查子帳號失敗（狀態 {result.get('status')}：{result.get('message')}），已停止")
    table = {}
    for entry in result.get("Account") or []:
        try:
            slot = int(str(entry["Name"]).rsplit("-", 1)[-1])
        except (KeyError, ValueError, TypeError):
            raise ValueError("子帳號清單內容異常，已停止") from None
        if not 1 <= slot <= TEAM_SIZE or not entry.get("PassWord"):
            raise ValueError("子帳號清單內容異常，已停止")
        table[str(slot)] = {"password_md5": entry["PassWord"]}
    if sorted(map(int, table)) != list(range(1, TEAM_SIZE + 1)):
        raise ValueError(f"帳號底下要剛好有 {TEAM_SIZE} 個子帳號（1～{TEAM_SIZE}），已停止")
    return table


def load_profile(env: Mapping[str, str]) -> tuple:
    """回傳（線路設定, 這次要跑的活動設定）。只收類型是練功的活動。"""
    path = Path(env["PROFILES_FILE"]) if env.get("PROFILES_FILE") else (
        MOUNTED_PROFILES if MOUNTED_PROFILES.is_file() else modules.PROFILE_FILE)
    try:
        profiles = modules.load_profiles(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"活動設定檔 {path} 讀不了：{error}") from None
    name = env.get("ACTIVITY") or "練功"
    profile = profiles.get(name)
    if not isinstance(profile, dict) or profile.get("類型") != "練功":
        raise ValueError(f"活動設定檔裡沒有練功活動「{name}」")
    profile = dict(profile)
    target, teleport = _number(env, "TARGET_LEVEL"), _number(env, "TELEPORT")
    if target is not None:
        profile["目標等級"] = target
    if teleport is not None:
        profile["傳送"] = teleport
    modules.fighter_of(modules.build(profile["模組"]))      # 模組名稱、相依有錯在這裡就擋下
    line = profiles.get("線路")
    if not isinstance(line, dict) or not {"port", "server_index"} <= set(line):
        raise ValueError("活動設定檔的「線路」要有 port 與 server_index")
    return line, profile


def map_dir_of(env: Mapping[str, str]) -> Path:
    path = Path(env["MAP_DIR"]) if env.get("MAP_DIR") else BUILTIN_MAPS
    try:
        MapGrid.load(path / "1000.dat")
    except OSError:
        raise ValueError(f"地圖目錄 {path} 裡找不到 1000.dat") from None
    return path


def api_token(env: Mapping[str, str]) -> str:
    token = _secret(env, "API_TOKEN")
    if token and len(token) < MIN_TOKEN:
        raise ValueError(f"API_TOKEN 至少要 {MIN_TOKEN} 個字元")
    return token


def build_config(env: Mapping[str, str], line: dict, fetch=fetch_subaccounts) -> dict:
    """組出登入用的設定（只在記憶體裡）。"""
    if env.get("TEST_ACCOUNT") != "1":
        raise ValueError("只接受測試帳號：確認是測試帳號請設 TEST_ACCOUNT=1")
    if env.get("ACCOUNT_FILE"):
        try:
            config = json.loads(Path(env["ACCOUNT_FILE"]).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ValueError("ACCOUNT_FILE 讀不了或不是 JSON") from None
        if config.get("test_only") is not True:
            raise ValueError("ACCOUNT_FILE 只接受測試帳號設定（test_only 要是 true）")
        missing = [key for key in ("host", "account", "subaccounts") if not config.get(key)]
        if missing:
            raise ValueError(f"ACCOUNT_FILE 缺少欄位：{'、'.join(missing)}")
    else:
        account, password = env.get("GAME_ACCOUNT", ""), _secret(env, "GAME_PASSWORD")
        missing = [name for name, value in (("GAME_ACCOUNT", account), ("GAME_PASSWORD", password),
                                            ("GAME_HOST", env.get("GAME_HOST")), ("GAME_API", env.get("GAME_API")))
                   if not value]
        if missing:
            raise ValueError(f"缺少環境變數：{'、'.join(missing)}")
        if not env["GAME_API"].startswith(("http://", "https://")):
            raise ValueError("GAME_API 要以 http:// 或 https:// 開頭")
        config = {"test_only": True, "host": env["GAME_HOST"], "account": account,
                  "subaccounts": fetch(env["GAME_API"], account, password)}
    config["port"], config["server_index"] = int(line["port"]), int(line["server_index"])
    return config


def with_retry(say):
    """回傳 attempt(步驟, 名稱)：步驟因為「連線逾時」失敗時，隔 RETRY_WAIT 秒再做，最多重做 RETRY_TIMES 次。
    使用者 2026-11-20 同意的例外（原則仍是出錯就停）：只對連線逾時；伺服器拒絕、數值異常、出現驗證照舊立刻停。
    只能包「重做也安全」的步驟（開局整備、配點、每輪收尾：做過的部分都會自動略過）。"""
    def attempt(step, what: str):
        for tried in range(RETRY_TIMES + 1):
            try:
                return step()
            except TimeoutError:
                if tried == RETRY_TIMES:
                    raise
                say(f"{what}連線逾時（第 {tried + 1} 次），{RETRY_WAIT} 秒後重做；最多重做 {RETRY_TIMES} 次")
                patient_sleep(RETRY_WAIT)
    return attempt


def discard_names(env: Mapping[str, str]) -> tuple:
    """開局要丟掉的寵物名稱（遊戲內原文，逗號分隔）；預設丟出生寵凱比（使用者 2026-11-20 指定），none＝不丟。"""
    text = env.get("DISCARD_PETS", "凯比")
    return () if text.strip().lower() in ("", "none") else tuple(part.strip() for part in text.split(",") if part.strip())


def settle_pets(config: dict, slot: int, discard: tuple, say) -> None:
    """這隻角色自己登入：設好騎寵與戰寵（名單見練功計畫檔），再把指定的寵物丟掉。
    騎寵、戰寵不會被丟；只丟 10 級以下的（丟寵不可逆）。伺服器沒確認就停。"""
    from . import newbie, quest
    session, outfitter = newbie._connect(config, slot)
    with session:
        session.request_roles()
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            session.poll_event(0.25)
        own = session.world.roles.controlled_slot
        session.ensure_pets(slots=(own,))
        actor = session.world.actor(own)
        names = {index: pet.name.decode("gbk", "replace") for index, pet in actor.pets.items()}
        say(f"開局整備 {slot} 號：騎 {names.get(actor.mount_pet_index)}、出戰 {names.get(actor.battle_pet_index)}")
        if discard:
            quest.discard_pets(session, say=lambda message: say(f"開局整備 {slot} 號：{message}"),
                               names=discard, maximum_level=DISCARD_MAX_LEVEL)


def opening(config: dict, say, outfit=None, pets=None, discard: tuple = ()) -> None:
    """新角色開局整備：五隻各自登入，穿新手裝備、開資源禮包、依角色屬性選自選蛋與騎乘證、取得騎乘資格，
    設騎寵與戰寵，丟掉指定的出生寵。做過的（道具已不在背包、寵物已設好）會自動略過；任何一步伺服器沒確認就停。
    智慧果留給之後的整備判斷要不要吃。"""
    from . import newbie
    outfit = outfit or newbie.outfit
    pets = pets or settle_pets
    attempt = with_retry(say)
    for slot in range(1, TEAM_SIZE + 1):
        check_stop()
        steps = attempt(lambda: outfit(config, slot, eat_fruit=False), f"開局整備 {slot} 號的道具")
        say(f"開局整備 {slot} 號：" + ("、".join(steps) if steps else "道具沒有要處理的"))
        time.sleep(OPENING_GAP)
        attempt(lambda: pets(config, slot, discard, say), f"開局整備 {slot} 號的寵物")
        time.sleep(OPENING_GAP)


def grow(env: Mapping[str, str], config: dict, map_dir: Path, target: int, say, supervisor=None):
    """低等階段：開局整備，再照分級路線（阿布洞穴 → 海底 → 山寨 → 地下一樓 → 漆黑洞穴 → 英雄島）練到 ROUTE_UNTIL。
    分級練功會自己組隊、設騎寵戰寵、配點、吃智慧果、補血、打不過退圖。
    回傳 None＝低等階段完成，可以接著跑活動設定檔的練功；回傳數字＝該停了（結束碼）。"""
    until = min(_number(env, "ROUTE_UNTIL", ROUTE_UNTIL), target)
    hours = _number(env, "ROUTE_HOURS", ROUTE_HOURS)
    if env.get("OPENING", "1") != "0":
        opening(config, say, discard=discard_names(env))
    if until <= 1:
        return None
    if supervisor is None:
        from .supervisor import Supervisor
        supervisor = Supervisor(config, map_dir=map_dir, target_level=until, maximum_seconds=hours * 3600.0,
                                log=lambda message: print(message, flush=True), sleep=patient_sleep)
        supervisor.before_battle = check_stop
        supervisor.fast = env.get("FAST_BATTLE", "1") != "0"
        supervisor.status_seconds = float(_number(env, "STATUS_SECONDS", STATUS_SECONDS))
    say(f"分級練功：全隊練到 {until} 級（時間上限 {hours} 小時）")
    with panels.activity("分級練功"):
        code = supervisor.run()
    level = supervisor.state.minimum_level
    if code != 0 or level is None or level < until:
        say(f"停止：分級練功沒有練到 {until} 級（結束碼 {code}，全隊最低 {level} 級）")
        return code or 1
    say(f"分級練功完成：全隊最低 {level} 級")
    return 0 if level >= target else None


def describe(env: Mapping[str, str]) -> str:
    """啟動時印的一行：只列有沒有設，不印值。"""
    names = ("GAME_ACCOUNT", "GAME_PASSWORD", "GAME_HOST", "GAME_API", "ACCOUNT_FILE", "API_TOKEN")
    states = [f"{name}={'有' if (env.get(name) or env.get(name + '_FILE')) else '沒有'}" for name in names]
    plain = [f"{name}={env[name]}" for name in ("ACTIVITY", "TARGET_LEVEL", "TELEPORT", "BATTLES", "OPENING",
                                                "ROUTE_UNTIL", "ROUTE_HOURS", "HOLD_ON_STOP") if env.get(name)]
    return "、".join(states + plain)


def data_writable() -> None:
    """面板快照的目錄要能寫（Kubernetes 掛載的磁碟區權限不對時，在登入前就擋下）。"""
    folder = panels.SNAPSHOT_FILE.parent
    probe = folder / ".write-test"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise ValueError(f"資料目錄 {folder} 不能寫：{error.strerror}（容器以使用者 65532 執行）") from None


def plan_readable() -> None:
    """分級練功要用的練功計畫檔（騎寵、戰寵名稱）讀得到。"""
    from .training import TrainingPlan
    TrainingPlan.load()


def snapshot_works() -> None:
    """用一筆假資料走一次「整理面板」：映像裡缺資料檔的話，在這裡就會發現，而不是登入後才停。"""
    from .world import WorldState
    world = WorldState()
    world.tasks = ("|".join(["1"] + ["0"] * 31).encode("ascii"), b"4-2")      # 任務 0 完成、任務 4 做到第 2 步
    rows = panels.snapshot_of(world).get("任務")
    if not rows:
        raise ValueError("面板整理不出任務進度")


def check(env: Mapping[str, str]) -> list:
    """不登入的自我檢查：設定檔、模組、地圖、協定表、面板整理、資料目錄。回傳問題清單（空的＝通過）。"""
    problems = []
    for step in (lambda: load_profile(env), lambda: map_dir_of(env), lambda: api_token(env),
                 lambda: _number(env, "BATTLES"), lambda: _number(env, "API_PORT"),
                 lambda: _number(env, "ROUTE_UNTIL"), lambda: _number(env, "ROUTE_HOURS"),
                 lambda: _number(env, "STATUS_SECONDS"), plan_readable, schemas,
                 snapshot_works, data_writable):
        try:
            step()
        except (ProtocolError, OSError, ValueError, KeyError) as error:
            problems.append(str(error))
    return problems


def main(argv=None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="只檢查設定與資料檔，不登入")
    args = parser.parse_args(argv)

    def say(message):
        print(time.strftime("%H:%M:%S"), message, flush=True)

    if args.check:
        problems = check(environ)
        for problem in problems:
            say(f"檢查沒過：{problem}")
        if not problems:
            say("檢查通過：活動設定、模組、地圖、協定表都讀得到")
        return 1 if problems else 0

    def on_term(signum, frame):
        _stop["requested"] = True      # 不在這裡直接中斷：等到安全的停止點（check_stop）才登出
        say("收到停止訊號：打完這一場就登出")

    servers = []
    _stop["requested"] = False
    try:
        signal.signal(signal.SIGTERM, on_term)
        code = _run(environ, say, lambda made: servers.append(made))
        if environ.get("HOLD_ON_STOP") == "1":
            # Kubernetes 的 Deployment 在容器結束後一定會重開、重新登入；留著不結束才守得住「出錯就停、不自動重試」。
            say("已停止並登出；HOLD_ON_STOP=1：容器留著不結束、不重新登入。看完日誌後刪掉 Pod 才會重新開始")
            while True:
                patient_sleep(3600)
        return code
    except Stopped:
        say("收到停止訊號：已登出並結束")
        return 0
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _run(environ: Mapping[str, str], say, opened) -> int:
    """跑一次練功；回傳結束碼。任何錯誤都只記一行原因，不重試。"""
    try:
        say(f"啟動：{describe(environ)}")
        line, profile = load_profile(environ)
        map_dir = map_dir_of(environ)
        token = api_token(environ)
        battles = _number(environ, "BATTLES", 6000)
        port = _number(environ, "API_PORT", API_PORT)
        snapshot_works()
        data_writable()
        config = build_config(environ, line)
        panels.clear_activity()
        if token:
            server = api.make_server("0.0.0.0", port, token=token, allow_refresh=False)
            opened(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            say(f"查詢 API 開在 {port} 埠（要帶 token）")
        else:
            say("沒設 API_TOKEN：不開查詢 API")
        code = grow(environ, config, map_dir, int(profile.get("目標等級", 140)), say)
        if code is not None:
            return code
        modules.BEFORE_BATTLE = check_stop
        modules.RETRY = with_retry(say)
        modules.STATUS_SECONDS = float(_number(environ, "STATUS_SECONDS", STATUS_SECONDS))
        code = modules.run_training(config, map_dir, profile, battles=battles, say=say)
        say("練功結束（到目標等級或打滿場數）" if code == 0 else f"練功結束，結束碼 {code}")
        return code
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        say(f"停止：{error}")
        return 1
    except Exception as error:      # 沒料到的錯誤也要留一行原因再結束，不重試
        say(f"停止：程式錯誤 {type(error).__name__}：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
