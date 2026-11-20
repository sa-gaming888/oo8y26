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


class Stopped(BaseException):
    """收到停止訊號（docker stop）。繼承 BaseException：不會被練功程式裡的錯誤處理吃掉。"""


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


def describe(env: Mapping[str, str]) -> str:
    """啟動時印的一行：只列有沒有設，不印值。"""
    names = ("GAME_ACCOUNT", "GAME_PASSWORD", "GAME_HOST", "GAME_API", "ACCOUNT_FILE", "API_TOKEN")
    states = [f"{name}={'有' if (env.get(name) or env.get(name + '_FILE')) else '沒有'}" for name in names]
    plain = [f"{name}={env[name]}" for name in ("ACTIVITY", "TARGET_LEVEL", "TELEPORT", "BATTLES") if env.get(name)]
    return "、".join(states + plain)


def check(env: Mapping[str, str]) -> list:
    """不登入的自我檢查：設定檔、模組、地圖、協定表。回傳問題清單（空的＝通過）。"""
    problems = []
    for step in (lambda: load_profile(env), lambda: map_dir_of(env), lambda: api_token(env),
                 lambda: _number(env, "BATTLES"), lambda: _number(env, "API_PORT"), schemas):
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
        raise Stopped()

    server = None
    try:
        signal.signal(signal.SIGTERM, on_term)
        say(f"啟動：{describe(environ)}")
        line, profile = load_profile(environ)
        map_dir = map_dir_of(environ)
        token = api_token(environ)
        battles = _number(environ, "BATTLES", 6000)
        port = _number(environ, "API_PORT", API_PORT)
        config = build_config(environ, line)
        panels.clear_activity()
        if token:
            server = api.make_server("0.0.0.0", port, token=token, allow_refresh=False)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            say(f"查詢 API 開在 {port} 埠（要帶 token）")
        else:
            say("沒設 API_TOKEN：不開查詢 API")
        return modules.run_training(config, map_dir, profile, battles=battles, say=say)
    except Stopped:
        say("收到停止訊號：已登出並結束")
        return 0
    except (ProtocolError, OSError, ValueError, KeyError) as error:
        say(f"停止：{error}")
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
