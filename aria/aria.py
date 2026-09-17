#!/usr/bin/env python3
"""
Aria - a small local app that turns YouTube (and many other) links into MP3 or MP4 files.

It starts a web server bound to 127.0.0.1, opens the default browser at it, and
drives yt-dlp + ffmpeg as subprocesses. Nothing leaves the machine except the
requests yt-dlp makes to the video site itself.

Runs as a plain script during development and as a frozen PyInstaller .exe in
production; the only difference is where the bundled web assets live.
"""

import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

APP_NAME = "Aria"
APP_VERSION = "1.1.0"
IS_WINDOWS = os.name == "nt"

# Where the tools come from. Both are fetched once on first run and then kept
# up to date in place, so the app keeps working as video sites change.
YTDLP_URLS = [
    "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp.exe",
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
]
YTDLP_CHANNEL = "nightly"
FFMPEG_ZIPS = [
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]
UPDATE_INTERVAL = 24 * 3600  # how often to let yt-dlp update itself

PORTS = range(8756, 8776)
IDLE_SHUTDOWN = 150  # seconds without a browser heartbeat before the app exits

# Videos longer than this get the smaller of the two automatic quality choices.
LONG_VIDEO_SECONDS = 45 * 60

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# YouTube serves its player through several "clients", and it rejects different
# ones over time - that is what "content is not available on this app" means.
# Rather than pin one and hope, try them in turn and remember what worked.
PLAYER_CLIENTS = [
    None,                     # whatever this build of yt-dlp defaults to
    "default,-tv_simply",
    "web_safari,web",
    "mweb",
    "tv_embedded",
    "android_vr",
    "ios",
]

# Failures worth retrying with a different client, as opposed to "this video is
# private", which no amount of retrying will fix.
CLIENT_TROUBLE = re.compile(
    r"not available on this app"
    r"|failed to extract any player response"
    r"|unable to extract (?:player|yt initial data|video data|initial player)"
    r"|invalid player client|unsupported client|no video formats found"
    r"|requested format is not available"
    r"|sign in to confirm",
    re.I,
)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def data_dir():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _windows_downloads():
    """Ask Windows where Downloads actually is - the folder is relocatable."""
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                    ("c", ctypes.c_ushort), ("d", ctypes.c_byte * 8)]

    guid = GUID()
    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
    if ole32.CLSIDFromString("{374DE290-123F-4565-9164-39C4925E467B}", ctypes.byref(guid)) != 0:
        return None
    ptr = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) != 0:
        return None
    path = ptr.value
    ole32.CoTaskMemFree(ptr)
    return path


def output_dir():
    downloads = None
    if IS_WINDOWS:
        try:
            downloads = _windows_downloads()
        except Exception:
            downloads = None
    if not downloads or not os.path.isdir(downloads):
        candidate = os.path.join(os.path.expanduser("~"), "Downloads")
        downloads = candidate if os.path.isdir(candidate) else os.path.expanduser("~")
    path = os.path.join(downloads, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def resource_dir():
    """Bundled read-only assets: the PyInstaller temp dir when frozen."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = data_dir()
BIN_DIR = os.path.join(DATA_DIR, "bin")
OUT_DIR = output_dir()
STATE_FILE = os.path.join(DATA_DIR, "state.json")

os.makedirs(BIN_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(DATA_DIR, "aria.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(APP_NAME)


def exe(name):
    return name + ".exe" if IS_WINDOWS else name


def tool(name):
    """Find a helper binary: our own bin dir first, then the system PATH."""
    local = os.path.join(BIN_DIR, exe(name))
    if os.path.exists(local):
        return local
    found = shutil.which(name)
    return found or local


def run_hidden(args, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    if not IS_WINDOWS:
        kwargs.pop("creationflags", None)
    return subprocess.run(args, **kwargs)


def popen_hidden(args, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    if not IS_WINDOWS:
        kwargs.pop("creationflags", None)
    return subprocess.Popen(args, **kwargs)


# --------------------------------------------------------------------------
# first-run setup: fetch yt-dlp and ffmpeg
# --------------------------------------------------------------------------

setup = {"state": "checking", "percent": 0, "message": "", "error": ""}


def _download(url, dest, on_progress=None):
    req = Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    tmp = dest + ".part"
    with urlopen(req, timeout=60) as response, open(tmp, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(262144)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(done / total)
    os.replace(tmp, dest)


def _extract_ffmpeg(zip_path):
    wanted = {"ffmpeg" + (".exe" if IS_WINDOWS else ""),
              "ffprobe" + (".exe" if IS_WINDOWS else "")}
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            base = os.path.basename(member)
            if base in wanted:
                with archive.open(member) as src, open(os.path.join(BIN_DIR, base), "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if not IS_WINDOWS:
                    os.chmod(os.path.join(BIN_DIR, base), 0o755)


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception:
        log.exception("could not save state")


updating = threading.Lock()


def update_tools():
    """Pull the newest yt-dlp on demand and forget the remembered client."""
    if not updating.acquire(blocking=False):
        return
    try:
        setup.update(state="updating", percent=0, message="", error="")
        result = run_hidden([tool("yt-dlp"), "--update-to", YTDLP_CHANNEL],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        log.info("update (%s): %s", result.returncode, output[-400:])
        if result.returncode != 0:
            setup.update(state="update-failed", error=output.splitlines()[-1][:200] if output else "")
            return
        state = _load_state()
        state["last_update"] = time.time()
        state.pop("player_client", None)  # let the carousel find the best one again
        _save_state(state)
        setup.update(state="updated", percent=100)
        threading.Timer(6, lambda: setup.update(state="ready")).start()
    except Exception as err:
        log.exception("update failed")
        setup.update(state="update-failed", error=str(err))
    finally:
        updating.release()


def client_order():
    """Clients to try, best-known-good first."""
    order = list(PLAYER_CLIENTS)
    remembered = _load_state().get("player_client", "\0")
    if remembered in order:
        order.remove(remembered)
        order.insert(0, remembered)
    return order


def remember_client(client):
    state = _load_state()
    if state.get("player_client", "\0") != client:
        state["player_client"] = client
        _save_state(state)
        log.info("player client that works here: %r", client)


def client_args(client):
    return ["--extractor-args", f"youtube:player_client={client}"] if client else []


def ensure_tools():
    """Make sure yt-dlp and ffmpeg exist, downloading them on first run."""
    state = _load_state()
    # an install made before Aria tracked the nightly channel is replaced once
    have_ytdlp = os.path.exists(tool("yt-dlp")) and state.get("channel") == YTDLP_CHANNEL
    have_ffmpeg = os.path.exists(tool("ffmpeg"))

    try:
        if not have_ytdlp:
            setup.update(state="downloading", percent=0, message="yt-dlp")
            target = os.path.join(BIN_DIR, exe("yt-dlp"))
            last_error = None
            for url in YTDLP_URLS:
                try:
                    _download(url, target, lambda f: setup.update(percent=round(f * 100)))
                    last_error = None
                    break
                except Exception as err:
                    last_error = err
                    log.warning("yt-dlp mirror failed %s: %s", url, err)
            if last_error:
                raise last_error
            if not IS_WINDOWS:
                os.chmod(target, 0o755)
            state["channel"] = YTDLP_CHANNEL
            state["last_update"] = time.time()
            _save_state(state)

        if not have_ffmpeg:
            setup.update(state="downloading", percent=0, message="ffmpeg")
            archive = os.path.join(BIN_DIR, "ffmpeg.zip")
            last_error = None
            for url in FFMPEG_ZIPS:
                try:
                    _download(url, archive, lambda f: setup.update(percent=round(f * 100)))
                    setup.update(state="extracting", percent=100, message="ffmpeg")
                    _extract_ffmpeg(archive)
                    last_error = None
                    break
                except Exception as err:  # try the next mirror
                    last_error = err
                    log.warning("ffmpeg mirror failed %s: %s", url, err)
            if os.path.exists(archive):
                os.remove(archive)
            if last_error:
                raise last_error

        setup.update(state="ready", percent=100, message="", error="")

        # Keep yt-dlp current; video sites change and a stale copy is the
        # single most common reason a downloader stops working.
        if time.time() - state.get("last_update", 0) > UPDATE_INTERVAL:
            try:
                run_hidden([tool("yt-dlp"), "-U"], capture_output=True, timeout=180)
            except Exception:
                log.warning("yt-dlp self-update skipped", exc_info=True)
            state["last_update"] = time.time()
            _save_state(state)

    except Exception as err:
        log.exception("setup failed")
        setup.update(state="error", error=str(err))


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

FRIENDLY_ERRORS = [
    (r"not available on this app|failed to extract any player response",
     "YouTube refused every connection method Aria knows. Press Update at the "
     "bottom of the window, then try again."),
    (r"private video", "This video is private."),
    (r"members[- ]only|join this channel", "This video is for channel members only."),
    (r"confirm your age|age[- ]restricted|inappropriate for some users",
     "This video is age-restricted, so it can't be downloaded."),
    (r"confirm (you|you're|youre).*not a bot|sign in to confirm",
     "The site asked to verify the request. Wait a minute and try again."),
    (r"video unavailable|has been removed|no longer available",
     "This video is unavailable - it may have been removed or blocked in your country."),
    (r"is not available in your country|geo restricted|geo-restricted",
     "This video is blocked in your country."),
    (r"unsupported url", "Aria doesn't recognise that link as a video."),
    (r"requested format is not available",
     "That quality isn't available for this video. Try Auto."),
    (r"unable to download webpage|getaddrinfo|temporary failure in name resolution|network is unreachable",
     "No internet connection."),
    (r"live event will begin|is live", "This is a live stream - wait until it has finished."),
    (r"no space left|not enough space", "The disk is full."),
]


def friendly(raw):
    blob = " ".join(raw).lower()
    for pattern, message in FRIENDLY_ERRORS:
        if re.search(pattern, blob):
            return message
    for line in reversed(raw):
        if "ERROR:" in line:
            return line.split("ERROR:", 1)[1].strip()[:300]
    return "The download failed. See aria.log for details."


class Job:
    def __init__(self, url, kind, quality):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.kind = kind            # "mp3" or "mp4"
        self.quality = quality      # "auto", "best" or "small"
        self.status = "queued"      # queued|probing|downloading|converting|done|error|canceled
        self.title = url
        self.channel = ""
        self.duration = 0
        self.thumbnail = ""
        self.percent = 0.0
        self.speed = 0.0
        self.eta = 0
        self.total = 0
        self.filepath = ""
        self.error = ""
        self.created = time.time()
        self.process = None
        self.cancelled = False

    def as_dict(self):
        return {
            "id": self.id, "url": self.url, "kind": self.kind, "quality": self.quality,
            "status": self.status, "title": self.title, "channel": self.channel,
            "duration": self.duration, "thumbnail": self.thumbnail,
            "percent": round(self.percent, 1), "speed": self.speed, "eta": self.eta,
            "total": self.total, "filepath": self.filepath, "error": self.error,
            "size": self.size(),
        }

    def size(self):
        try:
            return os.path.getsize(self.filepath) if self.filepath and os.path.isfile(self.filepath) else 0
        except OSError:
            return 0


jobs = {}
job_order = []
job_queue = queue.Queue()
jobs_lock = threading.Lock()


def probe(url):
    """Fetch title / duration / thumbnail without downloading anything."""
    failure = ""
    for client in client_order():
        args = ([tool("yt-dlp"), "-J", "--no-playlist", "--no-warnings", "--ignore-config",
                 "--socket-timeout", "20"] + client_args(client) + [url])
        result = run_hidden(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=90)
        if result.returncode == 0:
            remember_client(client)
            break
        failure = result.stderr or ""
        log.info("probe: player client %r did not work: %s", client, failure.strip()[-160:])
        if not CLIENT_TROUBLE.search(failure):
            break
    else:
        raise RuntimeError(friendly(failure.splitlines()))
    if result.returncode != 0:
        raise RuntimeError(friendly(failure.splitlines()))
    info = json.loads(result.stdout)
    if info.get("_type") == "playlist":  # a link that is only a playlist
        entries = info.get("entries") or []
        info = entries[0] if entries else info
    return {
        "title": info.get("title") or url,
        "channel": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
    }


def build_args(job, client=None):
    """Turn a job into a yt-dlp command line."""
    args = [
        tool("yt-dlp"), job.url,
        *client_args(client),
        "--ignore-config", "--no-playlist", "--newline", "--no-colors",
        "--no-simulate", "--progress",
        "--concurrent-fragments", "16",     # the single biggest speed win
        "--retries", "10", "--fragment-retries", "20",
        "--socket-timeout", "20",
        "--ffmpeg-location", BIN_DIR,
        "--paths", f"home:{OUT_DIR}",
        "--paths", f"temp:{os.path.join(OUT_DIR, '.part')}",
        "-o", "%(title)s.%(ext)s",
        "--trim-filenames", "180",
        "--embed-metadata",
        "--progress-template",
        "download:ARIA|%(progress.status)s|%(progress.downloaded_bytes)s|"
        "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
        "%(progress.speed)s|%(progress.eta)s",
        "--print", "after_move:ARIAFILE|%(filepath)s",
    ]
    if IS_WINDOWS:
        args.append("--windows-filenames")

    if job.kind == "mp3":
        quality = {"best": "0", "small": "5"}.get(job.quality)
        if quality is None:  # auto
            quality = "2" if job.duration > 90 * 60 else "0"
        args += ["-f", "ba[ext=m4a]/ba/b",
                 "-x", "--audio-format", "mp3", "--audio-quality", quality,
                 "--embed-thumbnail"]
    else:
        height = {"best": 1080, "small": 720}.get(job.quality)
        if height is None:  # auto
            height = 720 if job.duration > LONG_VIDEO_SECONDS else 1080
        args += ["-f", f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b",
                 "-S", "ext:mp4:m4a",
                 "--merge-output-format", "mp4"]
    return args


def run_job(job):
    if not job.duration or job.title == job.url:
        job.status = "probing"
        try:
            job.__dict__.update(probe(job.url))
        except Exception as err:
            job.status, job.error = "error", str(err)
            return

    tail = []
    for client in client_order():
        if job.cancelled:
            break
        job.percent, job.speed, job.eta = 0.0, 0.0, 0
        job.status = "downloading"
        code, tail = download_once(job, client)
        if job.cancelled:
            break
        if code == 0:
            remember_client(client)
            job.percent = 100.0
            job.status = "done"
            if not job.filepath:
                job.filepath = OUT_DIR
            return
        log.warning("job %s: player client %r failed with %s", job.id, client, code)
        if not CLIENT_TROUBLE.search("\n".join(tail)):
            break

    if job.cancelled:
        job.status = "canceled"
    else:
        job.status = "error"
        job.error = friendly(tail)
        log.error("job %s failed: %s", job.id, "\n".join(tail[-12:]))


def download_once(job, client):
    """Run yt-dlp once with one player client; returns (exit code, last output)."""
    args = build_args(job, client)
    log.info("job %s: %s", job.id, " ".join(args[1:]))

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    job.process = popen_hidden(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               bufsize=1, env=env)

    tail = []
    streams_finished = 0
    for line in job.process.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-60]

        if line.startswith("ARIA|"):
            _, status, done, total, estimate, speed, eta = (line.split("|") + ["NA"] * 6)[:7]
            total_bytes = _number(total) or _number(estimate)
            done_bytes = _number(done)
            if total_bytes:
                job.percent = min(99.9, done_bytes / total_bytes * 100)
                job.total = int(total_bytes)
            job.speed = _number(speed)
            job.eta = int(_number(eta))
            if status == "finished":
                streams_finished += 1
                # audio+video are two separate downloads; the tail of the work
                # after the last one is ffmpeg, which reports no progress
                job.status = "converting" if job.kind == "mp3" or streams_finished >= 2 else "downloading"
        elif line.startswith("ARIAFILE|"):
            job.filepath = line.split("|", 1)[1].strip()
        elif "has already been downloaded" in line:
            match = re.search(r"\[download\] (.+) has already been downloaded", line)
            if match:
                job.filepath = match.group(1).strip()
        elif line.startswith("[ExtractAudio]") or line.startswith("[Merger]"):
            job.status = "converting"

    code = job.process.wait()
    job.process = None
    return code, tail


def _number(text):
    try:
        value = float(text)
        return 0.0 if value != value else value  # NaN guard
    except (TypeError, ValueError):
        return 0.0


def worker():
    while True:
        job_id = job_queue.get()
        job = jobs.get(job_id)
        if job and not job.cancelled:
            try:
                run_job(job)
            except Exception as err:
                log.exception("job crashed")
                job.status, job.error = "error", str(err)
        job_queue.task_done()


def cancel(job):
    job.cancelled = True
    process = job.process
    if process and process.poll() is None:
        try:
            if IS_WINDOWS:
                run_hidden(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True)
            else:
                process.terminate()
        except Exception:
            log.warning("could not stop job %s", job.id, exc_info=True)
    if job.status in ("queued", "probing"):
        job.status = "canceled"


# --------------------------------------------------------------------------
# http server
# --------------------------------------------------------------------------

TOKEN = uuid.uuid4().hex
last_seen = time.time()
shutting_down = threading.Event()


def _reveal(path):
    """Open the containing folder, selecting the file when we have one."""
    target = path if path and os.path.exists(path) else OUT_DIR
    try:
        if IS_WINDOWS:
            if os.path.isfile(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if os.path.isfile(target) else "", target])
        else:
            subprocess.Popen(["xdg-open", target if os.path.isdir(target) else os.path.dirname(target)])
    except Exception:
        log.warning("could not open %s", target, exc_info=True)


def _play(path):
    if not path or not os.path.isfile(path):
        return
    try:
        if IS_WINDOWS:
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log.warning("could not play %s", path, exc_info=True)


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"

    def log_message(self, *args):  # keep the console quiet
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, bytes):
            payload = body
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorised(self):
        # Only our own page knows the token, so a random local page or another
        # program cannot drive the app.
        return self.headers.get("X-Aria-Token") == TOKEN

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        global last_seen
        route = urlparse(self.path)
        path = route.path

        if path in ("/", "/index.html"):
            last_seen = time.time()
            return self._send(200, self._page(), "text/html; charset=utf-8")

        if path == "/favicon.ico":
            try:
                with open(os.path.join(resource_dir(), "icon.ico"), "rb") as fh:
                    return self._send(200, fh.read(), "image/x-icon")
            except OSError:
                return self._send(404, b"", "image/x-icon")

        if path == "/api/ping":
            return self._send(200, {"app": APP_NAME, "version": APP_VERSION})

        if path == "/api/state":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            last_seen = time.time()
            with jobs_lock:
                items = [jobs[i].as_dict() for i in job_order if i in jobs]
            return self._send(200, {
                "setup": setup,
                "jobs": items,
                "outputDir": OUT_DIR,
                "version": APP_VERSION,
            })

        if path == "/api/probe":
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            url = (parse_qs(route.query).get("url") or [""])[0].strip()
            if not _valid_url(url):
                return self._send(400, {"error": "bad url"})
            try:
                return self._send(200, probe(url))
            except Exception as err:
                return self._send(200, {"error": str(err)})

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        global last_seen
        if not self._authorised():
            return self._send(403, {"error": "forbidden"})
        last_seen = time.time()
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/start":
            url = (body.get("url") or "").strip()
            kind = body.get("kind") if body.get("kind") in ("mp3", "mp4") else "mp3"
            quality = body.get("quality") if body.get("quality") in ("auto", "best", "small") else "auto"
            if not _valid_url(url):
                return self._send(400, {"error": "That doesn't look like a link."})
            job = Job(url, kind, quality)
            meta = body.get("meta") or {}
            job.title = meta.get("title") or url
            job.channel = meta.get("channel") or ""
            job.duration = int(meta.get("duration") or 0)
            job.thumbnail = meta.get("thumbnail") or ""
            with jobs_lock:
                jobs[job.id] = job
                job_order.append(job.id)
            job_queue.put(job.id)
            return self._send(200, job.as_dict())

        if path == "/api/cancel":
            job = jobs.get(body.get("id"))
            if job:
                cancel(job)
            return self._send(200, {"ok": True})

        if path == "/api/remove":
            job_id = body.get("id")
            with jobs_lock:
                job = jobs.pop(job_id, None)
                if job_id in job_order:
                    job_order.remove(job_id)
            if job and job.status in ("queued", "probing", "downloading", "converting"):
                cancel(job)
            return self._send(200, {"ok": True})

        if path == "/api/reveal":
            _reveal((jobs.get(body.get("id")) or Job("", "mp3", "auto")).filepath
                    if body.get("id") else OUT_DIR)
            return self._send(200, {"ok": True})

        if path == "/api/play":
            job = jobs.get(body.get("id"))
            if job:
                _play(job.filepath)
            return self._send(200, {"ok": True})

        if path == "/api/update":
            threading.Thread(target=update_tools, daemon=True).start()
            return self._send(200, {"ok": True})

        if path == "/api/quit":
            shutting_down.set()
            threading.Timer(0.4, _stop_server).start()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

    def _page(self):
        with open(os.path.join(resource_dir(), "web", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        return html.replace("__ARIA_TOKEN__", TOKEN).replace("__ARIA_VERSION__", APP_VERSION)


def _valid_url(url):
    try:
        parts = urlparse(url)
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    except Exception:
        return False


httpd = None


def _stop_server():
    if httpd:
        httpd.shutdown()


def idle_watchdog():
    """Quit when the browser tab is gone and there is nothing left to do."""
    while not shutting_down.is_set():
        time.sleep(10)
        with jobs_lock:
            busy = any(jobs[i].status in ("queued", "probing", "downloading", "converting")
                       for i in job_order if i in jobs)
        if not busy and time.time() - last_seen > IDLE_SHUTDOWN:
            log.info("idle, shutting down")
            _stop_server()
            return


def already_running():
    """If another copy is up, just bring its window forward instead."""
    for port in PORTS:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.6) as response:
                if json.loads(response.read().decode()).get("app") == APP_NAME:
                    return port
        except Exception:
            continue
    return None


def bind():
    for port in PORTS:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            server.daemon_threads = True
            return server, port
        except OSError:
            continue
    raise SystemExit("No free port for Aria")


def main():
    global httpd

    existing = already_running()
    if existing:
        webbrowser.open(f"http://127.0.0.1:{existing}/")
        return

    httpd, port = bind()
    log.info("%s %s starting on port %s, saving to %s", APP_NAME, APP_VERSION, port, OUT_DIR)

    threading.Thread(target=ensure_tools, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=idle_watchdog, daemon=True).start()

    if not os.environ.get("ARIA_NO_BROWSER"):
        webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log.info("stopped")


if __name__ == "__main__":
    main()
