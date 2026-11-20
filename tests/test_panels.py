"""面板整理與查詢 API 的測試（不連網）。訊息樣本取自 2026-11-20 的實錄，名字換掉。"""
import json
import time

import pytest

from oo8y26 import api, panels
from oo8y26.world import WorldState

P1 = ("P1|866|866|100|100|160|152|0|74|16731256|37698249|124|191|69|74|100|3|100|0|0|0|25240|20100|0|0|0|140|"
      "100010|768|测试员||100|0|1|141000|0|0|0|0|1794900209|1|0|0|0|0|0|0|0|0|0|0|0|0|0||0")
K1 = ("K1|1|108286|1139|1139|0|0|16726496|37698249|124|271|201|130|100|80|0|0|20|7|1|0|0|葛雷基欧||53|11|9|5|1|0|"
      "0,0|0,0|0,0|0,0|0|1139|271|201|130|0|-1|0|0|0|0|0|0|0|64636|9.878|2.266|1.626|1.187|0|0|0|0|0")
ITEM = "双倍智慧果(8小时)||0|经验加倍|24236|0|0|1|5|绑定物品|3|橡树果|20|1|2|0||||1|经验加倍|0|0"


class Event:
    def __init__(self, func, *values):
        self.func, self.values = func, values


def items_message(rows: dict) -> bytes:
    cells = []
    for slot in range(54):
        cells.extend((rows.get(slot) or "|" * 22).split("|"))
    return ("I" + "|".join(cells)).encode("gbk")


@pytest.fixture
def world():
    state = WorldState()
    state.apply(Event(46, b"C60049|100|100|60|8"))
    state.apply(Event(46, P1.encode("gbk")))
    state.apply(Event(46, K1.encode("gbk")))
    state.apply(Event(46, items_message({9: ITEM, 2: ITEM.replace("双倍智慧果(8小时)", "石斧")})))
    state.battle_pet_index = 1
    return state


def test_character_panel_names_known_fields(world):
    person = panels.character_panel(world)
    assert person["名稱"] == "测试员" and person["等級"] == 124 and person["石幣"] == 25240
    assert person["經驗"] == 16731256 and person["升級所需經驗"] == 37698249 and person["體力"] == 160
    assert person["地圖"] == 60049 and person["座標"] == [60, 8]
    assert person["未確認欄位"]["26"] == "140"          # 還沒確認含意的欄位原樣給出
    assert "聲望" not in person                         # 沒開過商城視窗就沒有聲望，不亂填
    world.apply(Event(262, 25240, 0, 73, 1580, 500, 0, 0))
    assert panels.character_panel(world)["聲望"] == 73


def test_partial_updates_reach_the_panel(world):
    world.apply(Event(46, "P6|307|866|".encode("gbk")))             # 位元 1、2：生命、最大生命
    assert panels.character_panel(world)["生命"] == 307
    world.apply(Event(46, b"K1|4|500|"))                            # 位元 2：寵物生命
    assert panels.pets_panel(world)[0]["生命"] == 500


def test_pets_and_items_panels(world):
    pet = panels.pets_panel(world)[0]
    assert pet["名稱"] == "葛雷基欧" and pet["等級"] == 124 and pet["出戰"] and not pet["騎乘"]
    assert pet["成長率"] == [9.878, 2.266, 1.626, 1.187]
    items = panels.items_panel(world)
    assert [item["名稱"] for item in items["裝備"]] == ["石斧"]
    assert items["背包"][0]["名稱"] == "双倍智慧果(8小时)" and items["背包"][0]["數量"] == 3
    assert items["背包空格"] == 14
    world.apply(Event(22, ("9|" + ITEM.replace("|3|橡树果", "|2|橡树果")).encode("gbk")))
    assert panels.items_panel(world)["背包"][0]["數量"] == 2


def test_record_merges_without_erasing_older_panels(world, tmp_path):
    path = tmp_path / "panels.json"
    panels.record(1, world, path)
    partial = WorldState()
    partial.apply(Event(46, P1.replace("|124|191|", "|125|191|").encode("gbk")))      # 只有人物，沒有道具
    panels.record(1, partial, path)
    entry = panels.load(path)["角色"]["1"]
    assert entry["人物"]["等級"] == 125 and entry["道具"]["背包空格"] == 14
    assert set(entry["更新時間"]) == {"人物", "寵物", "道具"}


def test_activity_marker_expires(tmp_path):
    path = tmp_path / "activity.json"
    assert panels.running_activity(path) is None
    panels.mark_activity("練功", path)
    assert panels.running_activity(path) == "練功"
    assert panels.running_activity(path, now=time.time() + panels.ACTIVITY_STALE + 1) is None
    with panels.activity("任務 64", path):
        assert panels.running_activity(path) == "任務 64"
    assert panels.running_activity(path) is None


def test_refresh_refuses_while_activity_runs(monkeypatch):
    monkeypatch.setattr(panels, "running_activity", lambda: "練功")
    with pytest.raises(RuntimeError, match="進行中"):
        panels.refresh({})


def test_api_routes(world, tmp_path, monkeypatch):
    path = tmp_path / "panels.json"
    panels.record(1, world, path)

    def get(target):
        return api.answer("GET", target, snapshot=path)

    assert get("/api")[0] == 200 and get("/nothing")[0] == 404
    status, rows = get("/api/summary")
    assert status == 200 and rows[0]["等級"] == 124 and rows[0]["石幣"] == 25240
    assert rows[0]["寵物"] == ["葛雷基欧 Lv124"]
    assert get("/api/characters/1/pets")[1]["寵物"][0]["名稱"] == "葛雷基欧"
    assert get("/api/characters/1/%E9%81%93%E5%85%B7")[1]["道具"]["背包空格"] == 14      # 中文路徑「道具」
    assert get("/api/characters/1/quests")[0] == 404          # 這條連線沒收到任務進度
    assert get("/api/characters/4")[0] == 404
    assert get("/api/currency")[1]["合計"]["石幣"] == 25240
    json.dumps(get("/api/characters")[1])
    assert api.answer("POST", "/api/refresh?sub=9", snapshot=path)[0] == 400
    monkeypatch.setattr(panels, "running_activity", lambda: "練功")
    assert api.answer("POST", "/api/refresh", snapshot=path)[0] == 409


def test_quests_panel_without_catalog_file(world, tmp_path, monkeypatch):
    """公開版容器不帶任務清單檔：任務面板只給進度，存快照不能因此停掉（2026-11-20 容器實際發生）。"""
    from oo8y26 import quest
    world.tasks = ("|".join(["1"] + ["0"] * 31).encode("ascii"), b"4-2")
    if quest.TASKS_FILE.is_file():
        assert any(row.get("名稱") for row in panels.quests_panel(world))        # 有清單檔：有名稱

    def missing(path=None):
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(quest, "load_tasks", missing)
    assert panels.quests_panel(world) == [{"編號": 0, "狀態": "已完成", "做完步數": None},
                                          {"編號": 4, "狀態": "進行中", "做完步數": 2}]
    panels.record(1, world, tmp_path / "panels.json")
    assert panels.load(tmp_path / "panels.json")["角色"]["1"]["任務"][1]["編號"] == 4
    assert api.answer("GET", "/api/summary", snapshot=tmp_path / "panels.json")[0] == 200
