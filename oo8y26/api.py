"""面板查詢 API（只聽本機）：人物、寵物、道具、任務、石幣、聲望。

    python -X utf8 -m oo8y26.api                 # http://127.0.0.1:8765

回的都是 JSON。資料來自快照檔（oo8y26/panels.py）：練功時每輪開頭五隻各自登入會更新全部面板，
之後每次進度回報更新人物與寵物；所以數字可能是幾分鐘前的，每個面板都附「更新時間」。

    GET  /api                         路徑一覽
    GET  /api/status                  目前有沒有活動在跑、快照檔位置
    GET  /api/summary                 五隻的摘要（等級、經驗、石幣、聲望、寵物、背包空格）
    GET  /api/characters              全部角色的全部面板
    GET  /api/characters/1            一號的全部面板
    GET  /api/characters/1/character  人物面板（另有 pets、items、quests；中文 人物、寵物、道具、任務 也可以）
    GET  /api/currency                五隻的石幣、聲望與合計
    POST /api/refresh                 五隻各自登入一次更新快照（?sub=3 只更新一隻）；練功或任務進行中會回 409
"""
from __future__ import annotations

import argparse
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import panels

SECTIONS = {"character": "人物", "pets": "寵物", "items": "道具", "quests": "任務",
            "人物": "人物", "寵物": "寵物", "道具": "道具", "任務": "任務"}
ROUTES = ["GET /api/status", "GET /api/summary", "GET /api/characters", "GET /api/characters/{1-5}",
          "GET /api/characters/{1-5}/{character|pets|items|quests}", "GET /api/currency",
          "POST /api/refresh[?sub=1-5]"]
_refresh_lock = threading.Lock()


def currency(data: dict) -> dict:
    rows = [{"角色": row["角色"], "名稱": row["名稱"], "石幣": row["石幣"], "聲望": row["聲望"]}
            for row in panels.summary(data)]
    total = {key: sum(row[key] for row in rows if isinstance(row[key], int)) for key in ("石幣", "聲望")}
    return {"角色": rows, "合計": total}


def answer(method: str, target: str, *, config_path: Path = None, snapshot: Path = None) -> tuple:
    """回傳（HTTP 狀態碼, 要轉成 JSON 的內容）。"""
    url = urlparse(target)
    parts = [unquote(part) for part in url.path.strip("/").split("/") if part]
    if not parts or parts[0] != "api":
        return 404, {"錯誤": "沒有這個路徑", "路徑": ROUTES}
    parts = parts[1:]
    data = panels.load(snapshot)
    if method == "POST":
        if parts != ["refresh"]:
            return 404, {"錯誤": "沒有這個路徑", "路徑": ROUTES}
        return refresh(parse_qs(url.query), config_path, snapshot)
    if not parts:
        return 200, {"路徑": ROUTES}
    if parts == ["status"]:
        return 200, {"進行中的活動": panels.running_activity(), "快照檔": str(snapshot or panels.SNAPSHOT_FILE),
                     "有資料的角色": sorted(int(sub) for sub in data["角色"])}
    if parts == ["summary"]:
        return 200, panels.summary(data)
    if parts == ["currency"]:
        return 200, currency(data)
    if parts[0] == "characters":
        if len(parts) == 1:
            return 200, data["角色"]
        entry = data["角色"].get(parts[1])
        if entry is None:
            return 404, {"錯誤": f"快照裡沒有角色 {parts[1]} 的資料"}
        if len(parts) == 2:
            return 200, entry
        section = SECTIONS.get(parts[2])
        if len(parts) == 3 and section is not None:
            if section not in entry:
                return 404, {"錯誤": f"角色 {parts[1]} 還沒有{section}面板的資料（要該角色單獨登入過才有）"}
            return 200, {section: entry[section], "更新時間": entry.get("更新時間", {}).get(section)}
    return 404, {"錯誤": "沒有這個路徑", "路徑": ROUTES}


def refresh(query: dict, config_path: Path = None, snapshot: Path = None) -> tuple:
    try:
        subs = [int(value) for value in query.get("sub", [])] or list(range(1, 6))
        if any(not 1 <= sub <= 5 for sub in subs):
            raise ValueError
    except ValueError:
        return 400, {"錯誤": "sub 要是 1～5"}
    running = panels.running_activity()
    if running:      # 先擋：活動進行中另外登入會把隊員拉離隊伍，連帳號設定都不用讀
        return 409, {"錯誤": f"「{running}」進行中，不能另外登入更新"}
    if not _refresh_lock.acquire(blocking=False):
        return 409, {"錯誤": "上一次更新還在進行"}
    try:
        config = json.loads((config_path or Path("config/query.local.json")).read_text(encoding="utf-8-sig"))
        if config.get("test_only") is not True:
            return 403, {"錯誤": "只接受測試帳號設定"}
        from .modules import load_profiles
        line = load_profiles().get("線路", {})
        config["port"] = line.get("port", config["port"])
        config["server_index"] = line.get("server_index", config["server_index"])
        done = panels.refresh(config, subs, snapshot, say=lambda message: None)
        return 200, {"已更新": done}
    except RuntimeError as error:
        return 409, {"錯誤": str(error)}
    except Exception as error:      # 連線錯誤照實回報，不重試
        return 502, {"錯誤": f"更新失敗：{error}"}
    finally:
        _refresh_lock.release()


def authorized(header, token) -> bool:
    """有設 token 時要帶 `Authorization: Bearer <token>`；沒設 token（只聽本機）一律放行。"""
    if not token:
        return True
    return hmac.compare_digest((header or "").encode("utf-8"), f"Bearer {token}".encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    config_path = None
    token = None            # 容器對外開放時必填（container.py）；本機用不設
    allow_refresh = True    # 容器裡關掉：帳號設定不在檔案裡，而且練功一直在跑

    def _reply(self, method):
        path = urlparse(self.path).path.rstrip("/")
        if method == "GET" and path == "/health":      # 給容器健康檢查用，不需要 token、不含遊戲資料
            status, body = 200, {"狀態": "執行中", "進行中的活動": panels.running_activity()}
        elif not authorized(self.headers.get("Authorization"), self.token):
            status, body = 401, {"錯誤": "要帶 Authorization: Bearer <token>"}
        elif method == "POST" and not self.allow_refresh:
            status, body = 403, {"錯誤": "這裡不開放 POST"}
        else:
            status, body = answer(method, self.path, config_path=self.config_path)
        payload = json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._reply("GET")

    def do_POST(self):
        self._reply("POST")

    def log_message(self, format, *args):      # 不把每個請求印到終端機
        pass


def make_server(host: str, port: int, *, token: str = None, allow_refresh: bool = True) -> ThreadingHTTPServer:
    """建立 API 伺服器（還沒開始服務）。對外開放（host 不是 127.0.0.1）一定要給 token。"""
    if host != "127.0.0.1" and not token:
        raise ValueError("對外開放查詢 API 一定要設 token")
    handler = type("Handler", (Handler,), {"token": token, "allow_refresh": allow_refresh,
                                           "config_path": Handler.config_path})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=Path("config/query.local.json"))
    args = parser.parse_args(argv)
    Handler.config_path = args.config
    server = make_server("127.0.0.1", args.port)      # 只聽本機，不對外
    print(f"面板查詢 API：http://127.0.0.1:{args.port}/api", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
