from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from flask import Flask, Response, g, jsonify, redirect, render_template, request
from werkzeug.exceptions import HTTPException
from yt_dlp import YoutubeDL


APP_HOST = "127.0.0.1"
APP_PORT = int(os.environ.get("YTMIGRATE_PORT", "8787"))
REDIRECT_URI = f"http://{APP_HOST}:{APP_PORT}/callback"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_ACCOUNTS = "https://accounts.spotify.com"
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_PLAYLIST_ITEMS = 5_000

app = Flask(__name__)
app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=2 * 1024 * 1024)

_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()


class AppError(Exception):
    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details


class YtDlpLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        self.errors.append(str(message))


@dataclass
class SourceTrack:
    index: int
    title: str
    artist: str
    duration_ms: int | None
    youtube_url: str


def _new_session() -> tuple[str, dict[str, Any]]:
    sid = secrets.token_urlsafe(32)
    data: dict[str, Any] = {"last_seen": time.time()}
    with _sessions_lock:
        _sessions[sid] = data
    return sid, data


def _cleanup_sessions() -> None:
    cutoff = time.time() - SESSION_TTL_SECONDS
    with _sessions_lock:
        stale = [sid for sid, data in _sessions.items() if data.get("last_seen", 0) < cutoff]
        for sid in stale:
            del _sessions[sid]


@app.before_request
def load_local_session() -> None:
    _cleanup_sessions()
    sid = request.cookies.get("ytmigrate_sid")
    with _sessions_lock:
        data = _sessions.get(sid) if sid else None
    if data is None:
        sid, data = _new_session()
        g.set_session_cookie = True
    else:
        g.set_session_cookie = False
    data["last_seen"] = time.time()
    g.sid = sid
    g.local_session = data


@app.after_request
def persist_local_session(response: Response) -> Response:
    if getattr(g, "set_session_cookie", False):
        response.set_cookie(
            "ytmigrate_sid",
            g.sid,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="Lax",
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(AppError)
def handle_app_error(error: AppError):
    payload: dict[str, Any] = {"error": error.message}
    if error.details is not None:
        payload["details"] = error.details
    return jsonify(payload), error.status


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("Unhandled error")
    return jsonify({"error": f"發生未預期的錯誤：{error}"}), 500


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise AppError("請提供有效的 JSON 請求。")
    return body


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(72)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _token_error(response: requests.Response, fallback: str) -> AppError:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text[:500]
    return AppError(fallback, response.status_code if response.status_code < 500 else 502, payload)


def _store_token(data: dict[str, Any], token: dict[str, Any]) -> None:
    data["access_token"] = token["access_token"]
    data["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    if token.get("refresh_token"):
        data["refresh_token"] = token["refresh_token"]


class SpotifyClient:
    def __init__(self, session_data: dict[str, Any]):
        self.data = session_data

    def _refresh_if_needed(self) -> None:
        if not self.data.get("access_token"):
            raise AppError("請先連結 Spotify。", 401)
        if self.data.get("expires_at", 0) > time.time() + 45:
            return
        refresh_token = self.data.get("refresh_token")
        client_id = self.data.get("client_id")
        if not refresh_token or not client_id:
            raise AppError("Spotify 登入已過期，請重新連結。", 401)
        response = requests.post(
            f"{SPOTIFY_ACCOUNTS}/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=30,
        )
        if not response.ok:
            raise _token_error(response, "Spotify 登入更新失敗，請重新連結。")
        _store_token(self.data, response.json())

    def request(self, method: str, path: str, _retry_auth: bool = True, **kwargs: Any) -> Any:
        self._refresh_if_needed()
        url = path if path.startswith("http") else f"{SPOTIFY_API}{path}"
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.data['access_token']}"

        for attempt in range(4):
            response = requests.request(method, url, headers=headers, timeout=35, **kwargs)
            if response.status_code == 429 and attempt < 3:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                reason = payload.get("error", {}).get("reason") if isinstance(payload, dict) else None
                if reason == "QUOTA_EXCEEDED":
                    raise AppError("Spotify 開發者 API 額度已用完，請稍後再試。", 429, payload)
                wait_seconds = min(float(response.headers.get("Retry-After", "2")), 20)
                time.sleep(max(wait_seconds, 0.5))
                continue
            break

        if response.status_code == 401:
            self.data["expires_at"] = 0
            if _retry_auth:
                return self.request(method, path, _retry_auth=False, **kwargs)
        if not response.ok:
            try:
                details = response.json()
            except ValueError:
                details = response.text[:800]
            message = "Spotify API 呼叫失敗。"
            if response.status_code == 403:
                message = "Spotify 拒絕此操作；請確認帳號已加入 App 的 Users Management，且 App 擁有需要的權限。"
            raise AppError(message, response.status_code if response.status_code < 500 else 502, details)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def profile(self) -> dict[str, Any]:
        return self.request("GET", "/me")

    def search_tracks(self, title: str, artist: str, broad_only: bool = False) -> list[dict[str, Any]]:
        clean_title = title.replace('"', " ").strip()
        clean_artist = artist.replace('"', " ").strip()
        if broad_only:
            query = f"{clean_title} {clean_artist}".strip()
        else:
            query = f'track:"{clean_title}"'
            if clean_artist:
                query += f' artist:"{clean_artist}"'
        payload = self.request("GET", "/search", params={"q": query, "type": "track", "limit": 10})
        items = payload.get("tracks", {}).get("items", [])

        if not items and not broad_only:
            payload = self.request(
                "GET",
                "/search",
                params={"q": f"{clean_title} {clean_artist}".strip(), "type": "track", "limit": 10},
            )
            items = payload.get("tracks", {}).get("items", [])
        return [item for item in items if item and item.get("uri")]

    def create_playlist(self, name: str, public: bool, description: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/me/playlists",
            json={"name": name, "public": public, "description": description[:300]},
        )

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for start in range(0, len(uris), 100):
            self.request(
                "POST",
                f"/playlists/{playlist_id}/items",
                json={"uris": uris[start : start + 100]},
            )


NOISE_PATTERNS = (
    "official audio",
    "official video",
    "official music video",
    "music video",
    "lyric video",
    "lyrics",
    "audio",
    "visualizer",
    "mv",
    "hd",
    "4k",
)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[\(\[【（](.*?)[\)\]】）]", _keep_useful_bracket, value)
    value = re.sub(r"\b(remaster(?:ed)?(?:\s+\d{4})?|mono|stereo|explicit)\b", " ", value)
    value = re.sub(r"[^\w\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _keep_useful_bracket(match: re.Match[str]) -> str:
    inside = match.group(1).casefold().strip()
    if any(noise in inside for noise in NOISE_PATTERNS) or re.search(r"\bremaster(?:ed)?\b", inside):
        return " "
    return f" {inside} "


def normalize_artist(value: str) -> str:
    value = re.sub(r"(?i)\s*[-–—]?\s*(topic|vevo|official)\s*$", "", value or "")
    return normalize_text(value)


def _similarity(left: str, right: str) -> float:
    left_n, right_n = normalize_text(left), normalize_text(right)
    if not left_n or not right_n:
        return 0.0
    sequence = SequenceMatcher(None, left_n, right_n).ratio()
    left_tokens, right_tokens = set(left_n.split()), set(right_n.split())
    token_score = (2 * len(left_tokens & right_tokens)) / (len(left_tokens) + len(right_tokens))
    containment = 0.92 if left_n in right_n or right_n in left_n else 0.0
    return max(sequence, token_score, containment)


def _split_artist_title(title: str, artist: str) -> tuple[str, str]:
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(parts) != 2:
        return title.strip(), artist.strip()
    left, right = (part.strip() for part in parts)
    if not artist or _similarity(left, artist) >= 0.65:
        return right, left
    if _similarity(right, artist) >= 0.65:
        return left, right
    # The uploader is often a label or curation channel rather than the song artist.
    # Keeping a wrong artist hurts Spotify search more than omitting it.
    return title.strip(), ""


def _clean_source_title(title: str, artist: str) -> str:
    parts = re.split(r"\s*[|｜]\s*", title)
    if len(parts) < 2:
        return title.strip()
    trailing = " ".join(parts[1:])
    trailing_normalized = normalize_text(trailing)
    noisy = any(
        phrase in trailing_normalized
        for phrase in ("official", "lyrics", "lyric video", "music video", "royalty free", "no copyright")
    )
    if noisy or (artist and _similarity(trailing, artist) >= 0.65):
        return parts[0].strip()
    return title.strip()


def score_candidate(source: SourceTrack, candidate: dict[str, Any]) -> dict[str, float]:
    title_score = _similarity(source.title, candidate.get("name", ""))
    candidate_artists = [item.get("name", "") for item in candidate.get("artists", [])]
    artist_score = max((_similarity(source.artist, artist) for artist in candidate_artists), default=0.0)
    duration_score = 0.0
    if source.duration_ms and candidate.get("duration_ms"):
        difference = abs(source.duration_ms - int(candidate["duration_ms"]))
        duration_score = max(0.0, 1.0 - difference / 45_000)

    if source.artist and source.duration_ms:
        total = 0.68 * title_score + 0.22 * artist_score + 0.10 * duration_score
    elif source.artist:
        total = 0.76 * title_score + 0.24 * artist_score
    elif source.duration_ms:
        total = 0.86 * title_score + 0.14 * duration_score
    else:
        total = title_score
    if title_score < 0.55:
        total *= 0.72
    return {
        "total": round(total, 4),
        "title": round(title_score, 4),
        "artist": round(artist_score, 4),
        "duration": round(duration_score, 4),
    }


def _youtube_url_ok(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if host not in {"music.youtube.com", "www.youtube.com", "youtube.com"}:
        return False
    return bool(parse_qs(parsed.query).get("list"))


def extract_youtube_playlist(url: str, cookies_browser: str = "none") -> tuple[dict[str, Any], list[SourceTrack]]:
    if not _youtube_url_ok(url):
        raise AppError("請貼上含有 list=... 的 YouTube Music／YouTube 播放清單網址。")
    yt_logger = YtDlpLogger()
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "logger": yt_logger,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "playlistend": MAX_PLAYLIST_ITEMS,
    }
    if cookies_browser in {"chrome", "edge", "firefox"}:
        options["cookiesfrombrowser"] = (cookies_browser, None, None, None)

    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as exc:
        hint = "若這是私人清單，請選擇已登入 YouTube Music 的瀏覽器後重試。"
        details = yt_logger.errors[-1] if yt_logger.errors else str(exc)
        raise AppError(f"無法讀取 YouTube Music 播放清單。{hint}", 422, details) from exc
    if not info:
        details = yt_logger.errors[-1] if yt_logger.errors else "YouTube Music 沒有回傳播放清單資料。"
        if "does not exist" in details.casefold():
            raise AppError(
                "YouTube 回報這個播放清單不存在。請從播放清單頁面的「分享 → 複製連結」重新取得完整網址。",
                422,
                details,
            )
        raise AppError("YouTube Music 沒有回傳播放清單資料。", 422, details)

    entries = list(info.get("entries") or [])
    tracks: list[SourceTrack] = []
    for entry in entries:
        if not entry:
            continue
        raw_title = str(entry.get("track") or entry.get("title") or "").strip()
        if not raw_title:
            continue
        raw_artists = entry.get("artists") or []
        if isinstance(raw_artists, list):
            artist_names = [
                str(item.get("name", "") if isinstance(item, dict) else item).strip()
                for item in raw_artists
            ]
            artist = ", ".join(name for name in artist_names if name)
        else:
            artist = ""
        artist = artist or str(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "").strip()
        raw_title = _clean_source_title(raw_title, artist)
        raw_title, artist = _split_artist_title(raw_title, artist)
        artist = re.sub(r"(?i)\s*[-–—]?\s*(topic|vevo|official)\s*$", "", artist).strip()

        duration = entry.get("duration")
        duration_ms = int(float(duration) * 1000) if duration else None
        video_id = entry.get("id") or ""
        webpage_url = entry.get("webpage_url") or entry.get("url") or ""
        if video_id and not str(webpage_url).startswith("http"):
            webpage_url = f"https://music.youtube.com/watch?v={video_id}"
        tracks.append(
            SourceTrack(
                index=len(tracks),
                title=raw_title,
                artist=artist,
                duration_ms=duration_ms,
                youtube_url=str(webpage_url),
            )
        )

    if not tracks:
        raise AppError("播放清單中找不到可辨識的歌曲。", 422)
    return {
        "title": str(info.get("title") or "YouTube Music 播放清單"),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "count": len(tracks),
        "source_url": url,
    }, tracks


def analyze_tracks(
    client: SpotifyClient,
    source_tracks: list[SourceTrack],
    threshold: float,
    progress_callback: Any = None,
    delay_seconds: float = 0.0,
    broad_search: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for position, source in enumerate(source_tracks, start=1):
        candidates = client.search_tracks(source.title, source.artist, broad_only=broad_search)
        ranked = []
        for candidate in candidates:
            scores = score_candidate(source, candidate)
            ranked.append((scores["total"], scores, candidate))
        ranked.sort(key=lambda item: item[0], reverse=True)
        match = None
        score = {"total": 0.0, "title": 0.0, "artist": 0.0, "duration": 0.0}
        if ranked:
            _, score, top = ranked[0]
            match = {
                "name": top.get("name", ""),
                "artists": ", ".join(item.get("name", "") for item in top.get("artists", [])),
                "album": top.get("album", {}).get("name", ""),
                "duration_ms": top.get("duration_ms"),
                "uri": top.get("uri"),
                "url": top.get("external_urls", {}).get("spotify", ""),
            }
        confident = bool(match and score["total"] >= threshold and score["title"] >= 0.62)
        results.append(
            {
                "source": asdict(source),
                "match": match,
                "score": score,
                "selected": confident,
                "confidence": "high" if score["total"] >= 0.86 else "medium" if confident else "low",
            }
        )
        if progress_callback:
            progress_callback(position, len(source_tracks))
        if delay_seconds:
            time.sleep(delay_seconds)
    return results


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _bulk_transfer_worker(session_data: dict[str, Any]) -> None:
    job = session_data["bulk_job"]
    try:
        job.update({"status": "extracting", "message": "正在讀取完整 YouTube Music 播放清單…", "error": None})
        if not job.get("_source_tracks"):
            playlist_meta, source_tracks = extract_youtube_playlist(
                job["source_url"],
                job["cookies_browser"],
            )
            job["_source_tracks"] = source_tracks
            job["_results"] = []
            job["playlist_meta"] = playlist_meta
            job["total"] = len(source_tracks)
            job["playlist_name"] = job["requested_name"] or playlist_meta["title"]

        source_tracks = job["_source_tracks"]
        client = SpotifyClient(session_data)
        if not job.get("spotify_playlist_id"):
            job.update({"status": "creating", "message": "正在建立 Spotify 播放清單…"})
            description = f"由 YouTube Music 自動分批轉移。來源：{job['source_url']}"
            playlist = client.create_playlist(job["playlist_name"], job["public"], description)
            job["spotify_playlist_id"] = playlist["id"]
            job["spotify_playlist_url"] = playlist.get("external_urls", {}).get("spotify", "")

        batch_size = int(job.get("batch_size", 100))
        total = len(source_tracks)
        start = int(job.get("next_index", 0))
        job["_cancel"] = False

        while start < total:
            if job.get("_cancel"):
                job.update({"status": "cancelled", "message": "已在上一個完成批次後停止。", "can_resume": True})
                return

            end = min(start + batch_size, total)
            batch = source_tracks[start:end]
            job.update(
                {
                    "status": "matching",
                    "batch_start": start + 1,
                    "batch_end": end,
                    "scanned": start,
                    "message": f"正在搜尋第 {start + 1}–{end} 首…",
                }
            )

            def update_progress(done: int, _batch_total: int) -> None:
                job["scanned"] = start + done
                job["message"] = f"正在搜尋第 {start + done}/{total} 首…"

            batch_results = analyze_tracks(
                client,
                batch,
                job["threshold"],
                progress_callback=update_progress,
                delay_seconds=0.08,
                broad_search=True,
            )
            uris: list[str] = []
            for item in batch_results:
                match = item.get("match")
                should_transfer = bool(match and (job["include_low_confidence"] or item["selected"]))
                item["transferred"] = should_transfer
                if should_transfer:
                    uris.append(match["uri"])

            job.update({"status": "adding", "message": f"正在加入第 {start + 1}–{end} 首到同一個 Spotify 清單…"})
            if uris:
                client.add_items(job["spotify_playlist_id"], uris)

            job["_results"].extend(batch_results)
            job["processed"] = end
            job["next_index"] = end
            job["added"] += len(uris)
            job["matched"] += sum(1 for item in batch_results if item.get("match"))
            job["unmatched"] += sum(1 for item in batch_results if not item.get("match"))
            job["low_confidence"] += sum(
                1 for item in batch_results if item.get("match") and not item.get("selected")
            )
            start = end

        analysis = {
            "playlist": job["playlist_meta"],
            "tracks": job["_results"],
            "threshold": job["threshold"],
            "created_at": time.time(),
        }
        session_data["analysis"] = analysis
        job.update(
            {
                "status": "complete",
                "processed": total,
                "scanned": total,
                "can_resume": False,
                "message": f"完成：已加入 {job['added']} 首，找不到 {job['unmatched']} 首。",
            }
        )
    except AppError as exc:
        job.update(
            {
                "status": "error",
                "error": exc.message,
                "error_details": exc.details,
                "message": "分批轉移已暫停；已完成的批次仍保留在 Spotify。",
                "can_resume": bool(job.get("_source_tracks") and job.get("spotify_playlist_id")),
                "scanned": job.get("processed", 0),
            }
        )
    except Exception as exc:
        app.logger.exception("Bulk transfer failed")
        job.update(
            {
                "status": "error",
                "error": f"發生未預期的錯誤：{exc}",
                "message": "分批轉移已暫停；已完成的批次仍保留在 Spotify。",
                "can_resume": bool(job.get("_source_tracks") and job.get("spotify_playlist_id")),
                "scanned": job.get("processed", 0),
            }
        )


@app.get("/")
def index():
    return render_template("index.html", redirect_uri=REDIRECT_URI)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/api/status")
def status():
    data = g.local_session
    return jsonify(
        {
            "authenticated": bool(data.get("access_token")),
            "profile": data.get("profile"),
            "has_analysis": bool(data.get("analysis")),
            "redirect_uri": REDIRECT_URI,
        }
    )


@app.post("/api/auth/start")
def auth_start():
    body = _json_body()
    client_id = str(body.get("client_id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", client_id):
        raise AppError("Spotify Client ID 格式不正確。")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    data = g.local_session
    data.update({"client_id": client_id, "pkce_verifier": verifier, "oauth_state": state})
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": "playlist-modify-private playlist-modify-public user-read-private",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return jsonify({"authorize_url": f"{SPOTIFY_ACCOUNTS}/authorize?{urlencode(params)}"})


@app.get("/callback")
def spotify_callback():
    data = g.local_session
    if request.args.get("error"):
        return redirect(f"/?auth=error&message={request.args.get('error')}")
    if not secrets.compare_digest(request.args.get("state", ""), data.get("oauth_state", "")):
        raise AppError("Spotify 登入狀態驗證失敗，請回首頁重新連結。", 400)
    code = request.args.get("code", "")
    if not code or not data.get("pkce_verifier") or not data.get("client_id"):
        raise AppError("Spotify 登入資料不完整，請重新連結。", 400)
    response = requests.post(
        f"{SPOTIFY_ACCOUNTS}/api/token",
        data={
            "client_id": data["client_id"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": data["pkce_verifier"],
        },
        timeout=30,
    )
    if not response.ok:
        raise _token_error(response, "無法完成 Spotify 登入。")
    _store_token(data, response.json())
    data["profile"] = SpotifyClient(data).profile()
    data.pop("pkce_verifier", None)
    data.pop("oauth_state", None)
    return redirect("/?auth=ok")


@app.post("/api/logout")
def logout():
    data = g.local_session
    if data.get("bulk_job"):
        data["bulk_job"]["_cancel"] = True
    for key in ("access_token", "refresh_token", "expires_at", "profile", "analysis", "bulk_job"):
        data.pop(key, None)
    return jsonify({"ok": True})


@app.post("/api/bulk/start")
def bulk_start():
    body = _json_body()
    data = g.local_session
    existing = data.get("bulk_job")
    if existing and existing.get("status") in {"queued", "extracting", "creating", "matching", "adding"}:
        raise AppError("已有大型清單轉移正在進行。", 409)

    url = str(body.get("playlist_url", "")).strip()
    if not _youtube_url_ok(url):
        raise AppError("請貼上含有 list=... 的 YouTube Music／YouTube 播放清單網址。")
    cookies_browser = str(body.get("cookies_browser", "none")).casefold()
    if cookies_browser not in {"none", "chrome", "edge", "firefox"}:
        raise AppError("Cookie 瀏覽器選項不正確。")
    try:
        threshold = float(body.get("threshold", 0.74))
    except (TypeError, ValueError) as exc:
        raise AppError("比對門檻格式錯誤。") from exc
    if not 0.5 <= threshold <= 0.95:
        raise AppError("比對門檻必須介於 0.50 到 0.95。")

    SpotifyClient(data)._refresh_if_needed()
    job = {
        "id": secrets.token_urlsafe(12),
        "status": "queued",
        "message": "準備開始…",
        "source_url": url,
        "cookies_browser": cookies_browser,
        "threshold": threshold,
        "include_low_confidence": bool(body.get("include_low_confidence", True)),
        "requested_name": str(body.get("name", "")).strip()[:100],
        "playlist_name": "",
        "public": bool(body.get("public", False)),
        "batch_size": 100,
        "total": 0,
        "processed": 0,
        "scanned": 0,
        "next_index": 0,
        "added": 0,
        "matched": 0,
        "unmatched": 0,
        "low_confidence": 0,
        "spotify_playlist_id": "",
        "spotify_playlist_url": "",
        "can_resume": False,
        "error": None,
        "error_details": None,
        "_cancel": False,
    }
    data["bulk_job"] = job
    threading.Thread(target=_bulk_transfer_worker, args=(data,), daemon=True).start()
    return jsonify(_public_job(job)), 202


@app.get("/api/bulk/status")
def bulk_status():
    job = g.local_session.get("bulk_job")
    if not job:
        return jsonify({"status": "idle"})
    return jsonify(_public_job(job))


@app.post("/api/bulk/resume")
def bulk_resume():
    data = g.local_session
    job = data.get("bulk_job")
    if not job:
        raise AppError("沒有可繼續的大型清單工作。", 404)
    if job.get("status") not in {"error", "cancelled"}:
        raise AppError("目前的工作不需要繼續。", 409)
    SpotifyClient(data)._refresh_if_needed()
    job.update({"status": "queued", "message": "準備從上一個完成批次繼續…", "error": None, "error_details": None})
    threading.Thread(target=_bulk_transfer_worker, args=(data,), daemon=True).start()
    return jsonify(_public_job(job)), 202


@app.post("/api/bulk/cancel")
def bulk_cancel():
    job = g.local_session.get("bulk_job")
    if not job:
        raise AppError("目前沒有大型清單工作。", 404)
    job["_cancel"] = True
    job["message"] = "將在目前批次結束後停止…"
    return jsonify(_public_job(job))


@app.post("/api/analyze")
def analyze():
    body = _json_body()
    url = str(body.get("playlist_url", "")).strip()
    cookies_browser = str(body.get("cookies_browser", "none")).casefold()
    try:
        threshold = float(body.get("threshold", 0.74))
    except (TypeError, ValueError) as exc:
        raise AppError("比對門檻格式錯誤。") from exc
    if not 0.5 <= threshold <= 0.95:
        raise AppError("比對門檻必須介於 0.50 到 0.95。")
    client = SpotifyClient(g.local_session)
    playlist, source_tracks = extract_youtube_playlist(url, cookies_browser)
    results = analyze_tracks(client, source_tracks, threshold)
    analysis = {"playlist": playlist, "tracks": results, "threshold": threshold, "created_at": time.time()}
    g.local_session["analysis"] = analysis
    return jsonify(analysis)


@app.post("/api/transfer")
def transfer():
    body = _json_body()
    analysis = g.local_session.get("analysis")
    if not analysis:
        raise AppError("請先分析播放清單。")
    name = str(body.get("name") or analysis["playlist"]["title"]).strip()[:100]
    if not name:
        raise AppError("請輸入 Spotify 播放清單名稱。")
    public = bool(body.get("public", False))
    requested = body.get("items")
    if not isinstance(requested, list):
        raise AppError("要轉移的歌曲清單格式不正確。")

    valid_uris = {
        item["match"]["uri"]
        for item in analysis["tracks"]
        if item.get("match") and re.fullmatch(r"spotify:track:[A-Za-z0-9]+", item["match"].get("uri", ""))
    }
    uris = [str(uri) for uri in requested if uri in valid_uris]
    if not uris:
        raise AppError("至少要勾選一首成功配對的歌曲。")

    client = SpotifyClient(g.local_session)
    description = f"由 YouTube Music 轉移，共 {len(uris)} 首。來源：{analysis['playlist']['source_url']}"
    playlist = client.create_playlist(name, public, description)
    try:
        client.add_items(playlist["id"], uris)
    except AppError as exc:
        partial_url = playlist.get("external_urls", {}).get("spotify", "")
        raise AppError(
            "播放清單已建立，但加入歌曲時中斷。可先開啟部分完成的清單後再重試。",
            exc.status,
            {"playlist_url": partial_url, "spotify_error": exc.details},
        ) from exc
    result = {
        "name": playlist.get("name", name),
        "url": playlist.get("external_urls", {}).get("spotify", ""),
        "id": playlist.get("id", ""),
        "added": len(uris),
        "skipped": len(analysis["tracks"]) - len(uris),
    }
    g.local_session["last_transfer"] = result
    return jsonify(result)


@app.get("/api/report.csv")
def report_csv():
    analysis = g.local_session.get("analysis")
    if not analysis:
        raise AppError("目前沒有可下載的分析報告。", 404)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["序號", "YouTube 歌名", "YouTube 藝人", "YouTube 網址", "Spotify 歌名", "Spotify 藝人", "Spotify 網址", "總分", "信心", "已轉移"]
    )
    for item in analysis["tracks"]:
        source, match = item["source"], item.get("match") or {}
        writer.writerow(
            [
                source["index"] + 1,
                source["title"],
                source["artist"],
                source["youtube_url"],
                match.get("name", ""),
                match.get("artists", ""),
                match.get("url", ""),
                item["score"]["total"],
                item["confidence"],
                "是" if item.get("transferred", item.get("selected", False)) else "否",
            ]
        )
    filename = re.sub(r"[^\w\-]+", "_", analysis["playlist"]["title"], flags=re.UNICODE).strip("_") or "report"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}_match_report.csv"'},
    )


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
