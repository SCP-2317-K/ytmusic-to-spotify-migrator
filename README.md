# YouTube Music → Spotify 播放清單轉移工具

> A local-first, open-source playlist migration tool with match review before transfer.

這是一個只在你電腦上執行的本機網頁工具。它會：

1. 用 `yt-dlp` 讀取 YouTube Music／YouTube 播放清單。
2. 依歌名、藝人與長度在 Spotify 搜尋並計算配對分數。
3. 先顯示預覽，讓你取消可疑配對。
4. 以 Spotify 官方 Web API 建立新播放清單。

Spotify Access Token、Refresh Token 與 YouTube Cookie 都不會寫入磁碟；關閉程式後即從記憶體消失。Client ID 只由瀏覽器的 Local Storage 記住。

## 從 GitHub 取得

已安裝 Git 時，可在終端機執行：

```bash
git clone https://github.com/SCP-2317-K/ytmusic-to-spotify-migrator.git
cd ytmusic-to-spotify-migrator
```

不熟悉 Git 也可以在 GitHub 專案頁按 **Code → Download ZIP**，解壓縮後再依下方方式執行。

## 隱私設計

- 專案不內建任何 Spotify Client ID 或 Client Secret，每位使用者建立並使用自己的 Spotify App。
- Spotify Token 只存在本機伺服器記憶體，不會寫入檔案或上傳到專案。
- 選用的 YouTube 瀏覽器 Cookie 由 `yt-dlp` 在本機讀取，不會傳送到此工具作者或 GitHub。
- 服務只監聽 `127.0.0.1`；這不是代管型雲端服務。

## 第一次設定

1. 登入 [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)。
2. 建立一個 App。
3. 在 App 的 Settings → Redirect URIs 加入：

   ```text
   http://127.0.0.1:8787/callback
   ```

   必須完全一致；Spotify 不接受 `localhost`，本機 HTTP 請使用 `127.0.0.1`。

4. 複製 App 的 Client ID。不要分享 Client Secret，本工具也完全不需要它。

注意：依 Spotify 目前的 Development Mode 規則，App 擁有者必須是 Premium 帳號。若登入者不是 App 擁有者，也要先在 App 的 Users Management 加入該使用者。

## Windows 執行方式

在此資料夾按住 Shift + 滑鼠右鍵，選「在終端機中開啟」，執行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start.ps1
```

第一次會建立 `.venv` 並安裝套件。之後瀏覽器會開啟 `http://127.0.0.1:8787`。啟動腳本可使用 `py`、`python` 或 Codex 隨附的 Python；若都沒有，請先安裝 Python 3.11 或更新版本。

也可以手動執行：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

停止工具：回到終端機按 `Ctrl+C`。

## macOS／Linux 執行方式

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

接著開啟 `http://127.0.0.1:8787`。Spotify App 的 Redirect URI 同樣設定為 `http://127.0.0.1:8787/callback`。

## 使用方式

1. 貼上 Client ID，按「連結 Spotify」並授權。
2. 貼上 YouTube Music 播放清單網址。
3. 公開清單選「不使用」。私人清單選擇目前已登入 YouTube Music 的 Chrome、Edge 或 Firefox。
4. 按「分析」，檢查每首歌的 Spotify 候選與分數。
5. 取消勾選錯誤配對，再建立播放清單。
6. 需要人工補歌時，可下載 CSV 報告。

### 超大型播放清單

像 2,792 首這類清單，請直接使用頁面中的「大型清單自動分批轉移」：

1. 貼上來源網址；公開清單的 Cookie 選「不使用」。
2. 若希望盡量加入所有歌曲，保留「加入所有找到的候選」勾選。這會加入低於門檻的候選，因此可能出現少量誤配。
3. 按「開始自動分批轉移」。工具只建立一個 Spotify 播放清單，每搜尋 100 首就加入一次。
4. 處理期間保持 PowerShell 程式開啟；網頁可以重新整理，進度仍會顯示。
5. 若遇到 Spotify API 額度或網路錯誤，已完成的批次不會重複加入。稍後按「從上次完成處繼續」即可寫入同一個播放清單。

YouTube 中有、但 Spotify 區域曲庫完全找不到的歌曲無法加入；完成後可下載 CSV 查看哪些歌曲缺少候選。大型模式目前支援最多 5,000 首，續傳資料只保留在正在執行的程式記憶體中，所以完成前不要關閉 PowerShell。

## 配對原則與限制

- 分數綜合歌名、藝人與播放長度；影片標題裡的 `Official Video`、`Lyrics`、`Remastered` 等標記會先清理。
- Live、翻唱、重新錄音、Remix、同名曲或地區限定版本仍可能誤配，所以工具刻意提供轉移前預覽。
- 私人清單讀取依賴瀏覽器 Cookie。若瀏覽器鎖住 Cookie 資料庫，完全關閉該瀏覽器後再試一次。
- 「喜歡的音樂」或系統自動清單可能受 YouTube 登入與頁面變更影響。
- Spotify API 有速率與 Development Mode 額度限制。大型清單如遇 `429`，稍後再試；工具會對一般速率限制自動短暫等待。
- 只搬移清單資料，不下載、不複製任何音訊。

## 技術與安全

- Spotify 登入採官方建議的 Authorization Code with PKCE。
- 服務只監聽 `127.0.0.1`，同網路中的其他裝置不能連入。
- 建立播放清單使用 2026 Web API 路徑：`POST /me/playlists` 與 `POST /playlists/{id}/items`。
- Spotify 搜尋每首最多取 10 個候選，再由本機程式排序。

官方參考：[PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)、[Redirect URI](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)、[Create Playlist](https://developer.spotify.com/documentation/web-api/reference/create-playlist)、[2026 API migration](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)。

## 開發與測試

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

GitHub Actions 會在 Python 3.11、3.12 與 3.13 自動執行測試。

## 授權

本專案採用 [MIT License](LICENSE)。你可以自由使用、修改與分享，但需保留授權聲明。
