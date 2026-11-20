# oo8y26

無畫面自動練功容器：啟動時帶入帳號密碼，程式自己登入帳號底下的五個角色、組隊、傳送到練功點練功，
全隊到目標等級就停。另外可以開一個唯讀查詢 API，查人物、寵物、道具、任務、石幣、聲望。

這個 repo 只放練功這一項功能，其他功能不在這裡。

## 啟動後會做什麼

1. **新角色開局整備**（做過的自動略過）：穿新手裝備、開資源禮包、依角色屬性選自選蛋與騎乘證、取得騎乘資格、
   設騎寵（馬年寵）與戰寵（人龍）、丟掉出生寵。單獨登入時若還卡在上次留下的戰鬥裡會先逃離。
2. **分級練功**到 `ROUTE_UNTIL` 級（預設 123）：未滿 10 級在村外練，之後依等級傳送
   阿布洞穴 → 海底 → 五兄弟山寨 → 地下一樓 → 漆黑洞穴 → 英雄島；打不過會退回上一張圖。
3. **活動設定檔的練功**（預設英雄島）練到目標等級。

只要是練功，下面這些都預設打開：原地遇敵、自動戰鬥、快速戰鬥、自動配點（有升級就配，兩次至少隔 5 分鐘）、
智慧果到期再吃、血量低回村外補血、背包快滿丟肉、血量低吃肉。

自選蛋的選法：選「角色屬性那一項數值最高」的寵物（馬年四隻依序是地、水、火、風；老虎與人龍沒有以水為主的）。
出生地支援薩姆吉爾與瑪麗娜絲漁村。

## 功能

練功由九個功能模組組成，開哪些、怎麼設定都寫在活動設定檔 [`data/module_profiles.json`](data/module_profiles.json)：

| 模組 | 做什麼 | 設定（預設） |
| :-- | :-- | :-- |
| 自動組隊 | 一號把隊員叫上線、召喚、組隊 | 隊員 [2,3,4,5] |
| 自動配點 | 升級點數依比例配 | 比例 [10,9,0,5]（體力、腕力、耐力、敏捷） |
| 自動吃智慧果 | 經驗加成到期再吃；身上沒有就從個人道具倉庫領 | 道具「双倍智慧果(8小时)」 |
| 自動丟肉 | 背包空格不夠就丟肉，地上放不下才吃掉 | 最少空格 3、清到空格 6 |
| 自動遇敵 | 原地遇敵，開打後取消後續遇敵 | 補送上限 5 |
| 自動戰鬥 | 全隊普通攻擊（快戰失效時的備援） | — |
| 快速戰鬥 | 開快戰開關，改用快戰打法 | — |
| 自動吃肉 | 一號或牠的寵物血量低於門檻就吃背包裡的肉 | 門檻 80 |
| 自動補血 | 全隊最低血量低於門檻就回村口補滿 | 補血門檻 50、停止門檻 30 |

一輪的流程：五隻各自登入收尾（打完殘留戰鬥、回記錄點、清背包）→ 配點、吃智慧果 → 組隊、傳送、開快戰 →
連續戰鬥 → 打滿「每輪場數」就重新連線，再來一輪。

- **出錯就停**：逾時、錯誤回應、數值對不上就記一行 `停止：…` 並結束（結束碼 1），**不自動重開**。
  請不要設 `--restart`；停下來先看日誌找原因。
- **只接受測試帳號**：要明確設 `TEST_ACCOUNT=1` 才會登入。
- **不碰付費項目**：只領免費的補血道具，不儲值、不買東西。
- 密碼只放在行程的記憶體，不寫檔、不進日誌。

## 快速開始

```bash
docker run -d --name bot \
  -e GAME_ACCOUNT=你的帳號 \
  -e GAME_PASSWORD=你的密碼 \
  -e GAME_HOST=遊戲伺服器位址 \
  -e GAME_API=http://帳號API位址:埠 \
  -e TEST_ACCOUNT=1 \
  -e TARGET_LEVEL=131 \
  -e API_TOKEN=至少16字元的隨機字串 \
  -p 127.0.0.1:8790:8790 \
  -v bot-data:/data \
  ghcr.io/sa-gaming888/oo8y26:latest

docker logs -f bot          # 看日誌（只印在 console）
docker stop bot             # 登出並結束；打到一半的戰鬥下次啟動時會先打完
```

密碼也可以用檔案帶入（docker secret）：`-e GAME_PASSWORD_FILE=/run/secrets/game_password`。

帳號底下要先有五個子帳號、每個都有角色（一級的新角色也可以），而且沒有同時在別處登入。

## 環境變數

| 變數 | 必填 | 說明 |
| :-- | :-: | :-- |
| `GAME_ACCOUNT` | ✔ | 帳號 |
| `GAME_PASSWORD` | ✔ | 密碼（或 `GAME_PASSWORD_FILE`） |
| `GAME_HOST` | ✔ | 遊戲伺服器位址 |
| `GAME_API` | ✔ | 帳號 API 位址（`http://主機:埠`）；啟動時用它查五個子帳號的登入憑證，只讀取 |
| `TEST_ACCOUNT` | ✔ | 一定要是 `1`：確認這是測試帳號 |
| `ACCOUNT_FILE` | | 改用掛載的帳號設定檔（JSON，已含子帳號憑證）；有設就不用上面四個帳號相關變數 |
| `ACTIVITY` | | 活動名稱，預設 `練功`（活動設定檔裡類型是練功的才收） |
| `TARGET_LEVEL` | | 目標等級，蓋過活動設定檔；全隊到了就停 |
| `TELEPORT` | | 傳送代號，蓋過活動設定檔 |
| `BATTLES` | | 最多打幾場，預設 6000 |
| `PROFILES_FILE` | | 活動設定檔路徑；預設有掛載 `/config/module_profiles.json` 就用，沒有用內建的 |
| `MAP_DIR` | | 地圖目錄；預設用內建的 `data/maps` |
| `API_TOKEN` | | 查詢 API 的 token（至少 16 字元，或 `API_TOKEN_FILE`）；有設才開 8790 埠 |
| `API_PORT` | | 查詢 API 的埠，預設 8790 |
| `OPENING` | | `0`＝不做新角色開局整備；預設會做，做過的自動略過 |
| `DISCARD_PETS` | | 開局要丟掉的寵物名稱（遊戲內原文，逗號分隔），預設 `凯比`（出生寵）；`none`＝不丟。騎寵、戰寵與 10 級以上的不丟，丟寵不可逆 |
| `ROUTE_UNTIL` | | 分級路線練到幾級才換成活動設定檔的練功點，預設 123；`1`＝不走分級路線 |
| `ROUTE_HOURS` | | 分級練功的時間上限（小時），預設 240 |
| `FAST_BATTLE` | | `0`＝分級練功不開快速戰鬥；預設開 |
| `HOLD_ON_STOP` | | `1`＝停下來之後（出錯、到目標等級）容器留著不結束，也不重新登入。**Kubernetes 上一定要設**：Deployment 在容器結束後會自動重開、重新登入。看完日誌後刪掉 Pod 才會重新開始 |

## 活動設定檔（掛載）

改模組或設定不用重建映像：

```bash
curl -fsSLo module_profiles.json https://raw.githubusercontent.com/sa-gaming888/oo8y26/main/data/module_profiles.json
docker run -d --name bot … -v ./module_profiles.json:/config/module_profiles.json:ro ghcr.io/sa-gaming888/oo8y26:latest
docker run --rm -v ./module_profiles.json:/config/module_profiles.json:ro ghcr.io/sa-gaming888/oo8y26:latest --check   # 先檢查
```

- 刪掉「模組」裡的一行就是關掉那個模組；在 `{}` 裡填值就是改設定，例如 `"自動補血": {"補血門檻": 60}`。
- 「線路」是登入的埠與線路編號；「傳送」是練功點的傳送代號；「每輪場數」打滿就重新連線。
- 設定檔讀不了、模組名稱不存在、缺少相依模組：**不登入**，日誌寫 `停止：…`。

## 查詢 API

```bash
TOKEN=你的API_TOKEN
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/api/summary
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/api/characters/1/pets
curl http://127.0.0.1:8790/health        # 不需要 token
```

| 路徑 | 內容 |
| :-- | :-- |
| `/api` | 路徑一覽 |
| `/api/status` | 目前有沒有活動在跑、快照裡有哪些角色 |
| `/api/summary` | 五隻摘要：等級、經驗、石幣、聲望、寵物、背包空格、已完成任務數 |
| `/api/currency` | 五隻的石幣、聲望與合計 |
| `/api/characters` | 全部角色的全部面板 |
| `/api/characters/1` | 一號的全部面板 |
| `/api/characters/1/character` | 人物面板（另有 `pets`、`items`、`quests`） |
| `/health` | 行程是否在跑（不需要 token，不含遊戲資料） |

只接受 GET，不會改變遊戲狀態。資料來自 `/data` 裡的快照，不是即時查詢，每個面板都附更新時間：
人物、寵物約每 20 場更新一次；道具、任務、聲望每輪開頭更新一次。

## 自己建映像

```bash
docker build -f Dockerfile.base -t oo8y26-base:local .
docker build --build-arg BASE=oo8y26-base:local -t oo8y26:local .
docker run --rm oo8y26:local --check
```

基底映像只有 Python、CA 憑證與時區資料，沒有 shell、沒有套件管理器，以非 root（65532）執行；
程式只用 Python 標準函式庫。`python -m pytest -q` 跑離線單元測試。
