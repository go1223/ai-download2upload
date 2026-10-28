#!/usr/bin/env python3
"""声取 MediaDock — 本地媒体下载与网易云播客上传服务。"""
from __future__ import annotations

import base64
import html
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import requests
import yt_dlp
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"
DOWNLOAD_DIR = BASE / "downloads"
UPLOAD_DIR = BASE / "uploads"
DATA_DIR = BASE / "data"
JOBS_FILE = DATA_DIR / "jobs.json"
for directory in (DOWNLOAD_DIR, UPLOAD_DIR, DATA_DIR):
    directory.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_DOWNLOAD_JOBS = 2
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BILI_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
NETEASE_NOS = "https://ymusic.nos-hz.163yun.com"
NETEASE_WEAPI = "https://interface.music.163.com/weapi"

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.json.ensure_ascii = False

download_slots = threading.Semaphore(MAX_DOWNLOAD_JOBS)


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 任务存储
# ---------------------------------------------------------------------------
class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.last_flush = 0.0
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for job in raw.get("jobs", []):
            if job.get("status") in {"queued", "running"}:
                job.update(
                    status="error",
                    error="服务重启，任务已中断，请重新发起",
                    stage="已中断",
                    updated_at=time.time(),
                )
            self.jobs[job["id"]] = job

    def _flush_locked(self):
        self.last_flush = time.time()
        tmp = self.path.with_suffix(".tmp")
        payload = {"jobs": sorted(self.jobs.values(), key=lambda x: x["created_at"], reverse=True)[:80]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def create(self, kind: str, title: str = "", mode: str = "", platform: str = "") -> dict:
        now = time.time()
        job = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "title": title,
            "mode": mode,
            "platform": platform,
            "status": "queued",
            "stage": "等待中",
            "progress": 0,
            "speed": None,
            "eta": None,
            "filename": None,
            "file_rel": None,
            "file_size": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.lock:
            self.jobs[job["id"]] = job
            self._flush_locked()
        return dict(job)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def update(self, job_id: str, **changes) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            old_status = job.get("status")
            old_file = job.get("filename")
            job.update(changes)
            job["updated_at"] = time.time()
            important = (
                old_status != job.get("status")
                or old_file != job.get("filename")
                or bool(job.get("error"))
            )
            if important or time.time() - self.last_flush > 0.8:
                self._flush_locked()
            return dict(job)

    def list(self, kind: str | None = None) -> list[dict]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda x: x["created_at"], reverse=True)
            if kind:
                jobs = [j for j in jobs if j.get("kind") == kind]
            return [dict(j) for j in jobs[:50]]


store = JobStore(JOBS_FILE)


def public_job(job: dict) -> dict:
    hidden = {"file_rel", "source_path", "voice_list_id"}
    data = {k: v for k, v in job.items() if k not in hidden}
    if job.get("status") == "done" and job.get("file_rel"):
        data["file_url"] = f"/api/jobs/{job['id']}/file"
    return data


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def ffmpeg_dir() -> str | None:
    candidates = []
    if os.getenv("FFMPEG_PATH"):
        candidates.append(Path(os.environ["FFMPEG_PATH"]))
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    candidates += [BASE / "bin" / "ffmpeg.exe", BASE / "bin" / "ffmpeg"]
    for item in candidates:
        try:
            path = item.expanduser().resolve()
        except Exception:
            continue
        if path.is_file() and path.stem.lower() == "ffmpeg":
            return str(path.parent)
        if path.is_dir() and (path / "ffmpeg.exe").exists():
            return str(path)
        if path.is_dir() and (path / "ffmpeg").exists():
            return str(path)
    return None


def safe_filename(name: str, limit: int = 80) -> str:
    name = html.unescape(re.sub(r"<[^>]+>", "", name or "")).strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:limit].strip() or "media")


def clean_error(exc: Exception | str) -> str:
    msg = str(exc)
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    msg = re.sub(r"^ERROR:\s*", "", msg.strip())
    msg = re.sub(r"\s+", " ", msg)
    return msg[:700] or "未知错误"


def normalize_cookie(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    raw = raw.replace("\r", "").replace("\n", "; ")
    if not re.search(r"(?:^|;\s*)(MUSIC_U|MUSIC_A)=", raw):
        raise AppError("Cookie 缺少 MUSIC_U，请复制登录后的完整 Cookie")
    return raw


def cookie_value(cookie: str, key: str) -> str:
    match = re.search(r"(?:^|;\s*)" + re.escape(key) + r"=([^;]*)", cookie)
    return match.group(1) if match else ""


def validate_url(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", raw):
        return f"https://www.bilibili.com/video/{raw}"
    if re.fullmatch(r"av\d+", raw, re.I):
        return f"https://www.bilibili.com/video/{raw}"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AppError("只支持 http/https 链接")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise AppError("不支持本地地址")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise AppError("不支持内网或保留 IP")
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise AppError("链接域名无法解析")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise AppError("链接指向内网地址，已拒绝")
    return raw


def make_bili_cookiefile(cookie: str, target_dir: Path) -> Path | None:
    cookie = (cookie or "").strip()
    if not cookie:
        return None
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    pairs = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
            continue
        pairs.append((key.strip(), value.strip()))
    if not pairs:
        return None
    path = target_dir / "cookies.txt"
    lines = ["# Netscape HTTP Cookie File", "# generated by MediaDock"]
    for key, value in pairs:
        lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\t{key}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class QuietLogger:
    def __init__(self):
        self.lines: list[str] = []

    def debug(self, msg):
        pass

    def warning(self, msg):
        self.lines.append(str(msg))

    def error(self, msg):
        self.lines.append(str(msg))


# ---------------------------------------------------------------------------
# B 站搜索
# ---------------------------------------------------------------------------
def bilibili_search(keyword: str, page: int, cookie: str = "") -> dict:
    keyword = (keyword or "").strip()
    if not keyword:
        raise AppError("请输入搜索关键词")
    if len(keyword) > 100:
        raise AppError("关键词过长")
    page = max(1, min(page, 50))
    session = requests.Session()
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie[7:].strip() if cookie.lower().startswith("cookie:") else cookie
    try:
        if not cookie:
            session.get("https://www.bilibili.com/", headers=headers, timeout=15)
        response = session.get(
            BILI_SEARCH_API,
            params={"search_type": "video", "keyword": keyword, "page": page},
            headers=headers,
            timeout=20,
        )
        payload = response.json()
    except requests.RequestException as exc:
        raise AppError(f"B 站搜索请求失败：{exc}") from exc
    except ValueError as exc:
        raise AppError("B 站搜索返回异常，请稍后重试") from exc

    code = payload.get("code")
    if code == -412:
        raise AppError("B 站触发风控，请在高级设置填入 B 站 Cookie 后重试")
    if code != 0:
        raise AppError(f"B 站搜索失败：{payload.get('message') or code}")

    data = payload.get("data") or {}
    items = []
    for row in data.get("result") or []:
        if row.get("type") != "video" or not row.get("bvid"):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", row.get("title") or ""))
        pic = row.get("pic") or ""
        if pic.startswith("//"):
            pic = "https:" + pic
        pubdate = row.get("pubdate")
        items.append(
            {
                "bvid": row["bvid"],
                "url": f"https://www.bilibili.com/video/{row['bvid']}",
                "title": title,
                "author": row.get("author") or "",
                "duration": row.get("duration") or "",
                "play": row.get("play") or 0,
                "pic": pic if pic.startswith(("http://", "https://")) else "",
                "pubdate": datetime.fromtimestamp(pubdate).strftime("%Y-%m-%d") if pubdate else "",
                "description": re.sub(r"<[^>]+>", "", row.get("description") or "")[:160],
            }
        )
    return {
        "items": items,
        "page": page,
        "pages": min(int(data.get("numPages") or 0), 50),
        "total": int(data.get("numResults") or 0),
    }


# ---------------------------------------------------------------------------
# 下载任务
# ---------------------------------------------------------------------------
def ydl_options(folder: Path, mode: str, cookie_file: Path | None = None) -> dict:
    opts = {
        "outtmpl": str(folder / "media.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noprecheck": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "logger": QuietLogger(),
        "color": "no_color",
    }
    ffdir = ffmpeg_dir()
    if ffdir:
        opts["ffmpeg_location"] = ffdir
    if cookie_file:
        opts["cookiefile"] = str(cookie_file)
    if mode == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ]
    else:
        opts["format"] = (
            "bv*[vcodec^=avc1]+ba[ext=m4a]/"
            "bv*[vcodec^=avc1]+ba/"
            "bv*+ba/b"
        )
        opts["postprocessors"] = [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ]
    return opts


def download_worker(job_id: str, url: str, mode: str, cookie: str) -> None:
    job = store.get(job_id)
    if not job:
        return
    folder = DOWNLOAD_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    cookie_file = None
    acquired = False
    try:
        store.update(job_id, status="queued", stage="排队中")
        acquired = download_slots.acquire(timeout=600)
        if not acquired:
            raise AppError("下载队列等待超时")
        store.update(job_id, status="running", stage="解析链接", progress=1)
        host = (urlparse(url).hostname or "").lower()
        if cookie and "bilibili.com" in host:
            cookie_file = make_bili_cookiefile(cookie, folder)

        def progress_hook(data):
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                done = data.get("downloaded_bytes") or 0
                pct = (done / total * 100) if total else 0
                current = (store.get(job_id) or {}).get("progress", 0)
                store.update(
                    job_id,
                    progress=round(max(current, min(90, 5 + pct * 0.85)), 1),
                    speed=data.get("speed"),
                    eta=data.get("eta"),
                    stage="下载中",
                )
            elif data.get("status") == "finished":
                store.update(job_id, progress=94, stage="处理文件", speed=None, eta=None)

        def postprocessor_hook(data):
            if data.get("status") == "started":
                store.update(job_id, progress=96, stage="合并 / 转码中", speed=None, eta=None)

        opts = ydl_options(folder, mode, cookie_file)
        opts["progress_hooks"] = [progress_hook]
        opts["postprocessor_hooks"] = [postprocessor_hook]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((x for x in info["entries"] if x), None)
            if not info:
                raise AppError("没有解析到可下载的媒体")

        ext = "mp3" if mode == "audio" else "mp4"
        candidates = sorted(folder.glob(f"*.{ext}"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise AppError(f"下载完成但未找到 .{ext} 文件，请检查 ffmpeg 是否安装")
        source = candidates[0]
        title = info.get("title") or info.get("id") or "media"
        final_name = f"{safe_filename(title)}.{ext}"
        final_path = folder / final_name
        if source.resolve() != final_path.resolve():
            os.replace(source, final_path)
        for leftover in folder.glob("media.*"):
            if leftover != final_path and leftover.suffix in {".webm", ".m4a", ".mp4", ".mp3", ".part"}:
                try:
                    leftover.unlink()
                except OSError:
                    pass
        platform = info.get("extractor_key") or info.get("extractor") or "yt-dlp"
        store.update(
            job_id,
            status="done",
            stage="已完成",
            progress=100,
            title=title,
            platform=platform,
            filename=final_name,
            file_rel=str(final_path.relative_to(BASE)),
            file_size=final_path.stat().st_size,
            speed=None,
            eta=None,
            error=None,
        )
    except Exception as exc:
        message = clean_error(exc)
        if "ffmpeg" in message.lower() and "not installed" in message.lower():
            message = "未找到 ffmpeg，无法合并视频/转码 MP3。请安装 ffmpeg 并加入 PATH。"
        store.update(job_id, status="error", stage="失败", error=message, speed=None, eta=None)
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        if acquired:
            download_slots.release()
        if cookie_file:
            try:
                cookie_file.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 网易云 weapi
# ---------------------------------------------------------------------------
AES_IV = b"0102030405060708"
AES_PRESET = b"0CoJUm6Qyw8W8jud"
BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
RSA_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935"
    "b3ece0462db0a22b8e7",
    16,
)
RSA_E = 65537


def _aes_b64(text: str, key: bytes) -> str:
    cipher = AES.new(key, AES.MODE_CBC, AES_IV)
    return base64.b64encode(cipher.encrypt(pad(text.encode("utf-8"), 16))).decode("ascii")


def weapi_encrypt(payload: dict, cookie: str) -> dict:
    secret = "".join(random.choice(BASE62) for _ in range(16))
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    params = _aes_b64(_aes_b64(raw, AES_PRESET), secret.encode("ascii"))
    number = int.from_bytes(secret[::-1].encode("ascii"), "big")
    enc_sec_key = format(pow(number, RSA_E, RSA_MODULUS), "0256x")
    return {"params": params, "encSecKey": enc_sec_key}


def weapi_post(path: str, payload: dict, cookie: str, timeout: int = 40, host: str = NETEASE_WEAPI) -> dict:
    cookie = normalize_cookie(cookie)
    payload = dict(payload)
    payload["csrf_token"] = cookie_value(cookie, "__csrf")
    headers = {
        "User-Agent": UA,
        "Referer": "https://music.163.com/",
        "Origin": "https://music.163.com",
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        response = requests.post(
            host + path,
            data=weapi_encrypt(payload, cookie),
            headers=headers,
            timeout=timeout,
        )
        result = response.json()
    except requests.RequestException as exc:
        raise AppError(f"网易云请求失败：{exc}") from exc
    except ValueError as exc:
        raise AppError("网易云返回异常，请稍后重试") from exc
    if result.get("code") == 301:
        raise AppError("网易云 Cookie 无效或已过期，请重新获取")
    if result.get("code") != 200:
        raise AppError(f"网易云接口错误：{result.get('msg') or result.get('message') or result.get('code')}")
    return result


def _podcast_rows(payload: dict) -> list:
    data = payload.get("data") or {}
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("data", "list", "voicelists"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = value.get("list") or value.get("data")
            if isinstance(nested, list):
                return nested
    return []


def netease_podcasts(cookie: str) -> list[dict]:
    """Read the signed-in creator's podcasts.

    NetEase's current web client uses social/my/created/voicelist/v1.
    Older accounts / API builds may still answer voicelist/search; keep it as
    a fallback so the page works across account variants.
    """
    try:
        result = weapi_post(
            "/social/my/created/voicelist/v1",
            {"limit": "100"},
            cookie,
        )
        rows = _podcast_rows(result)
    except AppError as exc:
        if "Cookie" in exc.message:
            raise
        result = weapi_post(
            "/voice/workbench/voicelist/search",
            {"fee": "-1", "limit": "200", "offset": "0", "podcastName": ""},
            cookie,
        )
        rows = _podcast_rows(result)
    if not rows:
        # Some older accounts only populate the workbench endpoint.
        try:
            result = weapi_post(
                "/voice/workbench/voicelist/search",
                {"fee": "-1", "limit": "200", "offset": "0", "podcastName": ""},
                cookie,
            )
            rows = _podcast_rows(result)
        except AppError:
            rows = []
    podcasts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        voice_id = row.get("voiceListId") or row.get("id")
        if not voice_id:
            continue
        cover = row.get("coverUrl") or row.get("cover") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        podcasts.append(
            {
                "voiceListId": str(voice_id),
                "name": row.get("voiceListName") or row.get("name") or "未命名播客",
                "cover": cover if cover.startswith(("http://", "https://")) else "",
                "description": re.sub(r"<[^>]+>", "", row.get("description") or "")[:200],
                "voiceCount": row.get("voiceCount") or row.get("programCount") or 0,
                "coverImgId": row.get("coverImgId") or row.get("coverImgIdStr") or "",
                "categoryId": row.get("categoryId") or "",
                "secondCategoryId": row.get("secondCategoryId") or "",
            }
        )
    return podcasts


def _nos_upload(session: requests.Session, token_data: dict, file_path: Path, progress) -> None:
    object_key = str(token_data["objectKey"]).replace("/", "%2F")
    token = token_data["token"]
    content_type = "audio/flac" if file_path.suffix.lower() == ".flac" else "audio/mpeg"
    base_headers = {
        "x-nos-token": token,
        "X-Nos-Meta-Content-Type": content_type,
    }
    init_url = f"{NETEASE_NOS}/{object_key}?uploads"
    init = session.post(init_url, headers=base_headers, timeout=60)
    if init.status_code >= 400 or "<UploadId>" not in init.text:
        raise AppError(f"网易云对象存储初始化失败（HTTP {init.status_code}）")
    upload_id = re.search(r"<UploadId>([^<]+)</UploadId>", init.text).group(1)
    size = file_path.stat().st_size

    class Reader:
        def __init__(self, fp):
            self.fp = fp
            self.sent = 0

        def read(self, amount=-1):
            chunk = self.fp.read(amount)
            if chunk:
                self.sent += len(chunk)
                progress(self.sent, size)
            return chunk

        def __len__(self):
            return size

    with file_path.open("rb") as fp:
        part = session.put(
            f"{NETEASE_NOS}/{object_key}?partNumber=1&uploadId={upload_id}",
            headers={**base_headers, "Content-Type": content_type, "Content-Length": str(size)},
            data=Reader(fp),
            timeout=(30, 300),
        )
    if part.status_code >= 400:
        raise AppError(f"音频上传失败（HTTP {part.status_code}）")
    etag = part.headers.get("ETag") or part.headers.get("etag")
    if not etag:
        raise AppError("网易云对象存储未返回 ETag")
    complete_xml = (
        "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber>"
        f"<ETag>{xml_escape(etag)}</ETag></Part></CompleteMultipartUpload>"
    )
    complete = session.post(
        f"{NETEASE_NOS}/{object_key}?uploadId={upload_id}",
        headers={**base_headers, "Content-Type": "text/plain;charset=UTF-8"},
        data=complete_xml.encode("utf-8"),
        timeout=120,
    )
    if complete.status_code >= 400:
        raise AppError(f"网易云对象存储合并失败（HTTP {complete.status_code}）")


def _voice_payload(job: dict, token_data: dict, auto_publish: bool) -> dict:
    voice = {
        "name": job["title"],
        "autoPublish": auto_publish,
        "autoPublishText": "",
        "description": job.get("description") or "",
        "voiceListId": str(job["voice_list_id"]),
        "dfsId": token_data.get("docId"),
        "composedSongs": [],
        "privacy": not auto_publish,
        "publishTime": 0,
        "orderNo": 1,
    }
    for key in ("coverImgId", "categoryId", "secondCategoryId"):
        value = job.get(key)
        if value not in (None, ""):
            voice[key] = str(value)
    return voice


def netease_upload_worker(job_id: str, cookie: str) -> None:
    job = store.get(job_id)
    if not job:
        return
    source = Path(job["source_path"])
    workdir = source.parent
    try:
        store.update(job_id, status="running", stage="准备音频", progress=3)
        cookie = normalize_cookie(cookie)
        if source.suffix.lower() not in {".mp3", ".flac"}:
            ffdir = ffmpeg_dir()
            if not ffdir:
                raise AppError("非 mp3/flac 文件需要 ffmpeg 转码，请先安装 ffmpeg")
            converted = workdir / "voice.mp3"
            store.update(job_id, stage="转码为 MP3", progress=5)
            proc = subprocess.run(
                [str(Path(ffdir) / "ffmpeg.exe" if os.name == "nt" else Path(ffdir) / "ffmpeg"),
                 "-y", "-i", str(source), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(converted)],
                capture_output=True,
                text=True,
                timeout=900,
            )
            if proc.returncode != 0 or not converted.exists():
                raise AppError("音频转码失败，请换用 mp3 文件")
            source = converted

        size = source.stat().st_size
        if size <= 0:
            raise AppError("音频文件为空")
        ext = "flac" if source.suffix.lower() == ".flac" else "mp3"
        session = requests.Session()
        session.headers.update({"User-Agent": UA})
        store.update(job_id, stage="申请上传凭证", progress=8)
        token_result = weapi_post(
            "/nos/token/alloc",
            {
                "bucket": "ymusic",
                "ext": ext,
                "filename": f"voice_{int(time.time())}",
                "local": False,
                "nos_product": 0,
                "type": "other",
            },
            cookie,
            host="https://music.163.com/weapi",
        )
        token_data = token_result.get("result") or token_result.get("data") or {}
        if not token_data.get("token") or not token_data.get("objectKey"):
            raise AppError("网易云未返回上传凭证")

        def progress(sent, total):
            current = (store.get(job_id) or {}).get("progress", 8)
            store.update(
                job_id,
                progress=round(max(current, 8 + (sent / max(total, 1)) * 80), 1),
                stage="上传音频",
            )

        store.update(job_id, stage="上传音频", progress=10)
        _nos_upload(session, token_data, source, progress)
        store.update(job_id, stage="提交节目信息", progress=92)
        auto_publish = bool(job.get("auto_publish"))
        voice = _voice_payload(job, token_data, auto_publish)
        headers = {"x-nos-token": token_data["token"]}
        precheck = weapi_post_with_headers(
            "/voice/workbench/voice/batch/upload/preCheck",
            {"dupkey": str(uuid.uuid4()), "voiceData": json.dumps([voice], ensure_ascii=False)},
            cookie,
            headers,
        )
        if precheck.get("code") not in (200, None):
            raise AppError(f"节目前置校验失败：{precheck.get('msg') or precheck.get('code')}")
        result = weapi_post_with_headers(
            "/voice/workbench/voice/batch/upload/v2",
            {"dupkey": str(uuid.uuid4()), "voiceData": json.dumps([voice], ensure_ascii=False)},
            cookie,
            headers,
        )
        store.update(
            job_id,
            status="done",
            stage="上传完成",
            progress=100,
            error=None,
            result=result.get("data") or {},
            source_path=None,
        )
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception as exc:
        store.update(
            job_id,
            status="error",
            stage="失败",
            error=clean_error(exc),
            source_path=None,
        )
        shutil.rmtree(workdir, ignore_errors=True)


def weapi_post_with_headers(path: str, payload: dict, cookie: str, extra_headers: dict) -> dict:
    cookie = normalize_cookie(cookie)
    payload = dict(payload)
    payload["csrf_token"] = cookie_value(cookie, "__csrf")
    headers = {
        "User-Agent": UA,
        "Referer": "https://music.163.com/",
        "Origin": "https://music.163.com",
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
        **extra_headers,
    }
    try:
        response = requests.post(
            NETEASE_WEAPI + path,
            data=weapi_encrypt(payload, cookie),
            headers=headers,
            timeout=120,
        )
        result = response.json()
    except requests.RequestException as exc:
        raise AppError(f"网易云请求失败：{exc}") from exc
    except ValueError as exc:
        raise AppError("网易云返回异常，请稍后重试") from exc
    if result.get("code") == 301:
        raise AppError("网易云 Cookie 无效或已过期，请重新获取")
    if result.get("code") != 200:
        raise AppError(f"网易云接口错误：{result.get('msg') or result.get('message') or result.get('code')}")
    return result


# ---------------------------------------------------------------------------
# Flask 路由
# ---------------------------------------------------------------------------
@app.after_request
def no_cache(response):
    if request.path == "/" or request.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(AppError)
def handle_app_error(exc: AppError):
    return jsonify({"error": exc.message}), exc.status


@app.errorhandler(413)
def handle_too_large(_):
    return jsonify({"error": f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"}), 413


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    if exc.code == 404:
        return jsonify({"error": "接口不存在" if request.path.startswith("/api/") else "页面不存在"}), 404
    return jsonify({"error": exc.description or exc.name}), exc.code or 500


@app.errorhandler(Exception)
def handle_unexpected(exc: Exception):
    app.logger.exception("Unhandled error")
    return jsonify({"error": f"服务内部错误：{clean_error(exc)}"}), 500


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/assets/<path:name>")
def assets(name: str):
    return send_from_directory(WEB_DIR, name)


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/api/health")
def health():
    ffdir = ffmpeg_dir()
    return jsonify(
        {
            "ok": True,
            "python": os.sys.version.split()[0],
            "yt_dlp": yt_dlp.version.__version__,
            "ffmpeg": bool(ffdir),
            "ffmpeg_path": ffdir or "",
            "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
        }
    )


@app.get("/api/search/bilibili")
def search_bilibili():
    keyword = request.args.get("q", "")
    page = request.args.get("page", "1")
    try:
        page_num = int(page)
    except ValueError:
        page_num = 1
    cookie = request.headers.get("X-Bili-Cookie", "")
    result = bilibili_search(keyword, page_num, cookie)
    return jsonify(result)


@app.post("/api/inspect")
def inspect_url():
    body = request.get_json(silent=True) or {}
    url = validate_url(body.get("url", ""))
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 25,
        "retries": 2,
        "color": "no_color",
        "logger": QuietLogger(),
    }
    ffdir = ffmpeg_dir()
    if ffdir:
        opts["ffmpeg_location"] = ffdir
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and info.get("entries"):
                info = next((x for x in info["entries"] if x), None)
    except Exception as exc:
        raise AppError(f"解析失败：{clean_error(exc)}") from exc
    if not info:
        raise AppError("没有解析到媒体信息")
    thumbnail = info.get("thumbnail") or ""
    if thumbnail.startswith("http://"):
        thumbnail = "https://" + thumbnail[7:]
    return jsonify(
        {
            "title": info.get("title") or "",
            "thumbnail": thumbnail,
            "duration": info.get("duration") or 0,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "platform": info.get("extractor_key") or info.get("extractor") or "",
            "url": info.get("webpage_url") or url,
        }
    )


@app.post("/api/download")
def create_download():
    body = request.get_json(silent=True) or {}
    url = validate_url(body.get("url", ""))
    mode = body.get("mode", "video")
    if mode not in {"video", "audio"}:
        raise AppError("下载类型必须是 video 或 audio")
    cookie = (body.get("cookie") or "").strip()
    ffdir = ffmpeg_dir()
    if not ffdir:
        raise AppError("未检测到 ffmpeg，无法合并视频或转码 MP3，请先安装 ffmpeg")
    job = store.create("download", title=(body.get("title") or "").strip()[:200], mode=mode, platform="")
    threading.Thread(target=download_worker, args=(job["id"], url, mode, cookie), daemon=True).start()
    return jsonify(public_job(job)), 202


@app.get("/api/jobs")
def list_jobs():
    kind = request.args.get("kind")
    return jsonify({"jobs": [public_job(j) for j in store.list(kind)]})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise AppError("任务不存在", 404)
    return jsonify(public_job(job))


@app.get("/api/jobs/<job_id>/file")
def download_file(job_id: str):
    job = store.get(job_id)
    if not job or not job.get("file_rel"):
        raise AppError("文件不存在", 404)
    path = (BASE / job["file_rel"]).resolve()
    if BASE not in path.parents or not path.exists():
        raise AppError("文件不存在", 404)
    return send_file(path, as_attachment=True, download_name=job.get("filename") or path.name, conditional=True)


@app.post("/api/netease/podcasts")
def podcasts():
    cookie = request.headers.get("X-Netease-Cookie", "")
    return jsonify({"podcasts": netease_podcasts(cookie)})


@app.post("/api/netease/upload")
def upload_podcast():
    cookie = request.headers.get("X-Netease-Cookie", "")
    normalize_cookie(cookie)
    voice_list_id = (request.args.get("voiceListId") or "").strip()
    if not voice_list_id:
        raise AppError("请选择目标播客")
    title = (request.args.get("title") or "").strip()
    if not title:
        raise AppError("请填写节目标题")
    if len(title) > 100:
        raise AppError("节目标题不能超过 100 个字符")
    description = (request.args.get("description") or "").strip()[:2000]
    filename = (request.args.get("filename") or "audio.mp3").strip()
    suffix = Path(filename).suffix.lower()
    if suffix not in {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg"}:
        raise AppError("仅支持 mp3 / flac / wav / m4a / aac / ogg 音频")
    content_length = request.content_length or 0
    if content_length <= 0:
        raise AppError("音频文件为空")
    if content_length > MAX_UPLOAD_BYTES:
        raise AppError(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限", 413)

    job = store.create("upload", title=title, mode="audio", platform="网易云播客")
    job_dir = UPLOAD_DIR / job["id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / f"source{suffix}"
    written = 0
    try:
        with target.open("wb") as fp:
            while True:
                chunk = request.stream.read(1024 * 1024)
                if not chunk:
                    break
                fp.write(chunk)
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise AppError(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限", 413)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        store.update(job["id"], status="error", stage="失败", error="文件接收失败")
        raise
    if written == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        store.update(job["id"], status="error", stage="失败", error="音频文件为空")
        raise AppError("音频文件为空")

    extra = {
        "source_path": str(target),
        "voice_list_id": voice_list_id,
        "description": description,
        "coverImgId": request.args.get("coverImgId", ""),
        "categoryId": request.args.get("categoryId", ""),
        "secondCategoryId": request.args.get("secondCategoryId", ""),
        "auto_publish": request.args.get("autoPublish", "0") == "1",
    }
    store.update(job["id"], **extra)
    threading.Thread(target=netease_upload_worker, args=(job["id"], cookie), daemon=True).start()
    return jsonify(public_job(store.get(job["id"]))), 202


def local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    if os.getenv("OPEN_BROWSER", "0") == "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print("=" * 58)
    print("  声取 MediaDock")
    print(f"  本机访问: http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"  局域网访问: http://{local_ip()}:{port}")
    print(f"  ffmpeg: {ffmpeg_dir() or '未找到（视频合并/MP3 会失败）'}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 58)
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
