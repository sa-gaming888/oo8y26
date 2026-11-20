"""容器進入點的測試（不連網）。帳密都是假的。"""
import json
import struct

import pytest

from oo8y26 import api, container, modules, panels

ENV = {"GAME_ACCOUNT": "tester", "GAME_PASSWORD": "pw-not-real", "GAME_HOST": "game.invalid",
       "GAME_API": "http://api.invalid:100", "TEST_ACCOUNT": "1"}
LINE = {"port": 9001, "server_index": 4}
MD5 = "0123456789abcdef0123456789abcdef"


def fake_api(accounts):
    calls = []

    def post(base, path, payload):
        calls.append((base, path, payload))
        if path.endswith("sign-in"):
            return {"status": 200, "Token": "t" * 40}
        return {"status": 200, "Account": accounts}
    return post, calls


def five():
    return [{"Name": f"tester-{slot}", "PassWord": MD5} for slot in range(1, 6)]


@pytest.fixture
def maps(tmp_path):
    (tmp_path / "1000.dat").write_bytes(struct.pack("<II", 2, 2) + bytes(2 * 2 * 6))
    return tmp_path


def test_fetch_subaccounts_reads_five():
    post, calls = fake_api(five())
    table = container.fetch_subaccounts("http://api.invalid:100", "tester", "pw-not-real", post=post)
    assert table == {str(slot): {"password_md5": MD5} for slot in range(1, 6)}
    assert [path for base, path, payload in calls] == ["/api/csa-account/sign-in", "/api/csa-account/account-list"]


@pytest.mark.parametrize("accounts", [five()[:4], five() + [{"Name": "tester-6", "PassWord": MD5}],
                                      [{"Name": "tester-1"}], [{"PassWord": MD5}], []])
def test_fetch_subaccounts_stops_on_odd_list(accounts):
    post, calls = fake_api(accounts)
    with pytest.raises(ValueError, match="已停止"):
        container.fetch_subaccounts("http://api.invalid:100", "tester", "pw-not-real", post=post)


def test_login_failure_message_has_no_password():
    def post(base, path, payload):
        return {"status": 401, "message": "密码错误"}
    with pytest.raises(ValueError) as caught:
        container.fetch_subaccounts("http://api.invalid:100", "tester", "pw-not-real", post=post)
    assert "pw-not-real" not in str(caught.value) and "401" in str(caught.value)


def test_build_config_from_environment():
    config = container.build_config(ENV, LINE, fetch=lambda base, account, password: {"1": {"password_md5": MD5}})
    assert config["host"] == "game.invalid" and config["port"] == 9001 and config["server_index"] == 4
    assert config["test_only"] is True and "password" not in config        # 密碼不留在設定裡


@pytest.mark.parametrize("change, message", [
    ({"TEST_ACCOUNT": "0"}, "測試帳號"), ({"GAME_PASSWORD": ""}, "GAME_PASSWORD"),
    ({"GAME_HOST": ""}, "GAME_HOST"), ({"GAME_API": "api.invalid"}, "http"),
])
def test_build_config_refuses(change, message):
    with pytest.raises(ValueError, match=message):
        container.build_config({**ENV, **change}, LINE, fetch=lambda *a: {})


def test_password_and_account_file(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("from-file\n", encoding="utf-8")
    seen = []
    env = {**ENV, "GAME_PASSWORD": "", "GAME_PASSWORD_FILE": str(secret)}
    container.build_config(env, LINE, fetch=lambda base, account, password: seen.append(password) or {})
    assert seen == ["from-file"]
    account = tmp_path / "account.json"
    account.write_text(json.dumps({"test_only": True, "host": "h", "port": 1, "server_index": 1, "account": "a",
                                   "subaccounts": {"1": {"password_md5": MD5}}}), encoding="utf-8")
    config = container.build_config({"TEST_ACCOUNT": "1", "ACCOUNT_FILE": str(account)}, LINE, fetch=None)
    assert config["port"] == 9001 and config["account"] == "a"            # 線路一律照活動設定檔
    account.write_text(json.dumps({"host": "h", "account": "a", "subaccounts": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="test_only"):
        container.build_config({"TEST_ACCOUNT": "1", "ACCOUNT_FILE": str(account)}, LINE, fetch=None)


def test_load_profile_only_training(tmp_path):
    line, profile = container.load_profile({"TARGET_LEVEL": "131", "TELEPORT": "106"})
    assert profile["類型"] == "練功" and profile["目標等級"] == 131 and profile["傳送"] == 106
    assert {"port", "server_index"} <= set(line)
    with pytest.raises(ValueError, match="正整數"):
        container.load_profile({"TARGET_LEVEL": "abc"})
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"線路": LINE, "任務": {"類型": "任務"},
                                "壞的": {"類型": "練功", "模組": {"沒有這個": {}}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="沒有練功活動"):
        container.load_profile({"PROFILES_FILE": str(path), "ACTIVITY": "任務"})
    with pytest.raises(ValueError, match="沒有這些模組"):
        container.load_profile({"PROFILES_FILE": str(path), "ACTIVITY": "壞的"})


def test_describe_never_prints_secrets():
    text = container.describe({**ENV, "API_TOKEN": "x" * 20, "TARGET_LEVEL": "131"})
    assert "pw-not-real" not in text and "tester" not in text and "game.invalid" not in text and "xxxx" not in text
    assert "GAME_PASSWORD=有" in text and "TARGET_LEVEL=131" in text


def test_check_reports_problems(maps, capsys):
    assert container.check({"MAP_DIR": str(maps)}) == []
    assert container.main(["--check"], {"MAP_DIR": str(maps)}) == 0
    problems = container.check({"MAP_DIR": str(maps / "none"), "API_TOKEN": "short", "ACTIVITY": "沒有"})
    assert len(problems) == 3
    assert container.main(["--check"], {"MAP_DIR": str(maps / "none")}) == 1


def test_main_runs_training_and_stops_on_error(maps, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(container.signal, "signal", lambda *a: None)
    monkeypatch.setattr(panels, "clear_activity", lambda: None)
    monkeypatch.setattr(container, "fetch_subaccounts", lambda *a: {"1": {"password_md5": MD5}})
    monkeypatch.setattr(container, "build_config",
                        lambda env, line: {"host": env["GAME_HOST"], "port": line["port"]})
    monkeypatch.setattr(modules, "run_training",
                        lambda config, map_dir, profile, battles, say: seen.update(config=config, battles=battles,
                                                                                    target=profile["目標等級"]) or 0)
    env = {**ENV, "MAP_DIR": str(maps), "TARGET_LEVEL": "131", "BATTLES": "50"}
    assert container.main([], env) == 0
    assert seen == {"config": {"host": "game.invalid", "port": 9001}, "battles": 50, "target": 131}
    output = capsys.readouterr().out
    assert "pw-not-real" not in output and "不開查詢 API" in output

    def stopped(*a, **k):
        raise container.Stopped()
    monkeypatch.setattr(modules, "run_training", stopped)
    assert container.main([], env) == 0 and "收到停止訊號" in capsys.readouterr().out

    def broken(*a, **k):
        raise container.ProtocolError("逾時，已停止")
    monkeypatch.setattr(modules, "run_training", broken)
    assert container.main([], env) == 1 and "停止：逾時" in capsys.readouterr().out
    assert container.main([], {**env, "TEST_ACCOUNT": ""}) == 1


def test_api_token_and_health(tmp_path):
    assert api.authorized(None, None) and api.authorized("Bearer " + "k" * 16, "k" * 16)
    assert not api.authorized(None, "k" * 16) and not api.authorized("Bearer wrong", "k" * 16)
    with pytest.raises(ValueError, match="token"):
        api.make_server("0.0.0.0", 0)
    server = api.make_server("127.0.0.1", 0, token="k" * 16, allow_refresh=False)
    try:
        import threading
        import urllib.error
        import urllib.request
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def status(path, token=None, method="GET"):
            request = urllib.request.Request(base + path, method=method,
                                             headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                return urllib.request.urlopen(request, timeout=5).status
            except urllib.error.HTTPError as error:
                return error.code
        assert status("/health") == 200 and status("/api") == 401 and status("/api", "k" * 16) == 200
        assert status("/api/refresh", "k" * 16, "POST") == 403
    finally:
        server.shutdown()
        server.server_close()


def test_hold_on_stop_keeps_container_alive(maps, monkeypatch, capsys):
    """Kubernetes 上容器結束會被重開、重新登入：HOLD_ON_STOP=1 時停下來就留著，直到收到停止訊號。"""
    monkeypatch.setattr(container.signal, "signal", lambda *a: None)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise container.Stopped()
    monkeypatch.setattr(container.time, "sleep", sleep)
    env = {**ENV, "MAP_DIR": str(maps), "TEST_ACCOUNT": "", "HOLD_ON_STOP": "1"}       # 設定有錯：登入前就停
    assert container.main([], env) == 0 and len(sleeps) == 3
    output = capsys.readouterr().out
    assert "停止：只接受測試帳號" in output and "不重新登入" in output and "收到停止訊號" in output
    assert container.main([], {**env, "HOLD_ON_STOP": "0"}) == 1 and len(sleeps) == 3   # 沒設：照常結束
