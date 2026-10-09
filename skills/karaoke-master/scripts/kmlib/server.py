"""Local web server for the karaoke-master workflow page.

Stdlib only (``http.server``) so it starts instantly.  Responsibilities:

* serve the single page app in ``web/``;
* serve job files (media with HTTP Range so <video>/<audio> can seek);
* push job / chat / log changes to the page with Server-Sent Events;
* accept user actions: drafts are stored directly, deterministic tool actions
  (style previews, timing nudges, exports) are executed by spawning
  ``km.py ui-action`` and everything else is queued in ``inbox.jsonl`` for the
  agent (``km.py wait``).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .jobstore import JobStore, read_json, write_json
from .paths import SCRIPTS_DIR, WEB_DIR, hidden_subprocess_kwargs


class _QuietServer(ThreadingHTTPServer):
    """Browsers drop connections all the time (reloads, aborted media range
    requests); don't print a traceback for those."""

    closing: str | None = None
    # Windows SO_REUSEADDR lets a second server bind a port that is already in use
    # (two projects would answer on the same URL); bind exclusively instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            import socket

            self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        super().server_bind()

    def request_close(self, by: str) -> None:
        """Let open pages know (SSE "closed"), then stop serving."""
        self.closing = by

        def later() -> None:
            time.sleep(1.2)
            self.shutdown()

        threading.Thread(target=later, daemon=True).start()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("image/webp", ".webp")

# Actions the server runs itself through ``km.py ui-action`` (no agent needed).
TOOL_ACTIONS = {
    "preview_styles",   # re-render template / effect / singer previews
    "preview_frame",    # engine-render one frame at time t (stage 3)
    "edit_timing",      # manual nudges / 平滑走字 from the review table
    "singers",          # stage-3 singer palette: add / recolour, assign lines (several = 拼色 chorus)
    "undo",             # restore the timing project before the last manual change
    "set_option",       # change a render option from the review page
    "export",           # .sug / .yurika / mp4
    "confirm_stage1",   # apply the user's stage-1 decisions (agent is notified too)
    "set_hires_source", # register / clear the stage-1 Hi-Res source (aligned to the video)
    "mv_preset",        # pick an MV theme preset (re-renders the design stills)
    "mv_stills",        # re-render the MV design stills
    "mv_gallery",       # render one still per MV preset
    "mv_asset_remove",  # drop montage images the user doesn't want (re-renders the stills)
    "mv_plan",          # recompute the beat-synced cut plan
    "set_background",   # stage-1 background choice: video / AMV / image montage / subtitles only
    "montage_source",   # where montage images come from: web / user packs / mixed
    "mv_assets_import", # import an uploaded image pack (files, folders, .zip)
    "mv_video_search",  # search YouTube for the song's MV (no download)
    "mv_video_use",     # download the chosen MV and align it to the song (the user clicked it)
    "mv_video_local",   # use a local video file as the MV background (aligned)
}
# Tool actions the agent must also react to.
NOTIFY_AGENT = {"confirm_stage1", "set_background", "montage_source", "mv_assets_import", "mv_video_use",
                "mv_video_local"}
# Actions that only touch drafts or the OS.
LOCAL_ACTIONS = {"save_draft", "open_folder", "open_file", "open_app", "close_page"}


def lock_path(job_dir: str | Path) -> Path:
    """``<project>/live_preview/lock.json``: url / port / pid of the project's page server."""
    return Path(job_dir).resolve() / "live_preview" / "lock.json"


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def running(job_dir: str | Path) -> dict | None:
    info = read_json(lock_path(job_dir))
    if not info or not info.get("pid") or not _alive(info["pid"]):
        return None
    try:
        import urllib.request

        with urllib.request.urlopen(info["url"] + "api/ping", timeout=2) as r:
            if json.loads(r.read() or b"{}").get("job_dir") == str(Path(job_dir).resolve()):
                return info
    except Exception:
        return None
    return None


def serve_daemon(job_dir: str | Path, port: int = 0, open_browser: bool = True) -> dict:
    """Start the project's page server in the background (or reuse the running one)."""
    info = running(job_dir)
    if info:
        if open_browser:
            import webbrowser

            webbrowser.open(info["url"])
        return {**info, "reused": True}
    lock_path(job_dir).unlink(missing_ok=True)
    cmd = [sys.executable, str(SCRIPTS_DIR / "km.py"), "serve", str(Path(job_dir).resolve())]
    if port:
        cmd += ["--port", str(port)]
    if not open_browser:
        cmd.append("--no-browser")
    live = Path(job_dir).resolve() / "live_preview"
    live.mkdir(parents=True, exist_ok=True)
    log = open(live / "server.log", "a", encoding="utf-8")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    deadline = time.time() + 20
    while time.time() < deadline:
        info = running(job_dir)
        if info:
            return {**info, "reused": False}
        time.sleep(0.25)
    raise SystemExit("网页服务未能启动，见 " + str(Path(job_dir).resolve() / "live_preview" / "server.log"))


def stop(job_dir: str | Path) -> dict:
    """Close the project's page server (the page shows that it has ended)."""
    info = read_json(lock_path(job_dir))
    if not info:
        return {"stopped": False, "reason": "本工程没有正在运行的网页服务"}
    try:
        import urllib.request

        req = urllib.request.Request(info["url"] + "api/shutdown", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass
    deadline = time.time() + 6
    while time.time() < deadline and info.get("pid") and _alive(info["pid"]):
        time.sleep(0.2)
    if info.get("pid") and _alive(info["pid"]):
        try:
            os.kill(int(info["pid"]), 9 if os.name != "nt" else 1)
        except OSError:
            pass
    lock_path(job_dir).unlink(missing_ok=True)
    return {"stopped": True, "url": info.get("url")}

_running_tools: dict[str, subprocess.Popen] = {}
_tools_lock = threading.Lock()


def _spawn_tool(store: JobStore, action: dict) -> None:
    action_file = store.path("logs", f"action_{action['id']}.json")
    write_json(action_file, action)
    cmd = [sys.executable, str(SCRIPTS_DIR / "km.py"), "ui-action", "--job", str(store.dir),
           "--action-file", str(action_file)]
    log = open(store.path("logs", "ui_actions.log"), "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(store.dir),
                            **hidden_subprocess_kwargs())
    with _tools_lock:
        _running_tools[action["id"]] = proc


class Handler(BaseHTTPRequestHandler):
    server_version = "KaraokeMaster/1.0"
    store: JobStore  # injected

    # silence default stderr logging
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        pass

    # ------------------------------------------------------------ helpers
    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _serve_file(self, path: Path, *, cache: bool = False) -> None:
        if not path.is_file():
            self._send_error(404, "not found")
            return
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "text/javascript"):
            ctype += "; charset=utf-8"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _job_payload(self) -> dict:
        store = self.store
        job = store.load()
        drafts = {}
        drafts_dir = store.dir / "drafts"
        if drafts_dir.is_dir():
            for f in drafts_dir.glob("*.json"):
                drafts[f.stem] = read_json(f)
        return {
            "job": job,
            "chat": store.chat_history(),
            "drafts": drafts,
            "pending_actions": sum(1 for a in store.pending_actions() if a.get("handled_by", "agent") != "server"),
            "job_dir": str(store.dir),
        }

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = urllib.parse.unquote(parsed.path)
        if route in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "index.html")
        elif route.startswith("/static/"):
            target = (WEB_DIR / route[len("/static/"):]).resolve()
            if WEB_DIR.resolve() not in target.parents:
                return self._send_error(403, "forbidden")
            self._serve_file(target)
        elif route == "/api/job":
            try:
                self._send_json(self._job_payload())
            except FileNotFoundError as exc:
                self._send_error(404, str(exc))
        elif route == "/api/events":
            self._sse()
        elif route == "/api/ping":
            self._send_json({"ok": True, "job_dir": str(self.store.dir), "pid": os.getpid()})
        elif route.startswith("/files/"):
            rel = route[len("/files/"):]
            target = (self.store.dir / rel).resolve()
            if self.store.dir not in target.parents:
                return self._send_error(403, "forbidden")
            self._serve_file(target)
        elif route == "/api/log":
            log = self.store.dir / "logs" / "events.log"
            lines = log.read_text(encoding="utf-8").splitlines()[-400:] if log.exists() else []
            self._send_json({"lines": lines})
        elif route == "/favicon.ico":
            self._serve_file(WEB_DIR / "favicon.svg")
        else:
            self._send_error(404, "not found")

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        store = self.store
        watched = {
            "job": store.dir / "job.json",
            "chat": store.dir / "chat.jsonl",
            "log": store.dir / "logs" / "events.log",
            "inbox": store.dir / "inbox.cursor",
        }
        mtimes: dict[str, float] = {}
        log_pos = 0
        log_path = watched["log"]
        if log_path.exists():
            log_pos = max(0, log_path.stat().st_size - 8000)
        last_beat = time.monotonic()
        try:
            while True:
                if self.server.closing:
                    self._emit("closed", {"by": self.server.closing})
                    return
                for key, path in watched.items():
                    try:
                        mt = path.stat().st_mtime_ns
                    except FileNotFoundError:
                        continue
                    if mtimes.get(key) == mt:
                        continue
                    first = key not in mtimes
                    mtimes[key] = mt
                    if key == "log":
                        with open(path, "rb") as fh:
                            fh.seek(log_pos)
                            data = fh.read()
                            log_pos = fh.tell()
                        text = data.decode("utf-8", "replace")
                        for line in text.splitlines():
                            self._emit("log", {"line": line})
                    elif not first or key == "job":
                        self._emit("job", self._job_payload())
                if time.monotonic() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                time.sleep(0.35)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return

    def _emit(self, event: str, data) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    # --------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        store = self.store
        if route == "/api/shutdown":
            self._send_json({"ok": True})
            self.server.request_close("agent")
            return
        if route == "/api/action":
            try:
                action = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._send_error(400, "bad json")
            kind = action.get("type")
            if not kind:
                return self._send_error(400, "missing type")
            if kind == "save_draft":
                store.save_draft(action.get("name", "draft"), action.get("payload"))
                return self._send_json({"ok": True})
            if kind == "close_page":
                # the user ended the project in the page: tell the agent, then stop serving
                store.post_action({"type": "page_closed", "payload": {}, "handled_by": "agent"})
                store.log("用户在网页中结束了本工程，网页服务已关闭")
                self._send_json({"ok": True})
                self.server.request_close("user")
                return
            if kind == "open_app":
                from .commands import open_app

                try:
                    res = open_app(store, (action.get("payload") or {}).get("file"))
                except SystemExit as exc:
                    return self._send_error(400, str(exc))
                return self._send_json({"ok": True, **res})
            if kind in ("open_folder", "open_file"):
                rel = (action.get("payload") or {}).get("path", "")
                target = (store.dir / rel).resolve() if rel else store.dir
                out_dir = (store.load().get("options") or {}).get("output_dir")
                roots = [store.dir] + ([Path(out_dir).resolve()] if out_dir else [])
                if not any(r == target or r in target.parents for r in roots):
                    return self._send_error(403, "forbidden")
                if os.name == "nt":
                    if kind == "open_folder" and target.is_file():
                        subprocess.Popen(["explorer", "/select,", str(target)])
                    else:
                        os.startfile(str(target))  # noqa: S606
                return self._send_json({"ok": True})
            handled = "agent"
            if kind in TOOL_ACTIONS:
                handled = "server+agent" if kind in NOTIFY_AGENT else "server"
            entry = store.post_action({**{k: v for k, v in action.items() if k != "id"}, "handled_by": handled})
            if kind in TOOL_ACTIONS:
                _spawn_tool(store, entry)
                return self._send_json({"ok": True, "id": entry["id"], "handled_by": "server"})
            if kind == "prompt":
                store.chat("user", str((action.get("payload") or {}).get("text", "")))
            return self._send_json({"ok": True, "id": entry["id"], "handled_by": "agent"})
        if route == "/api/upload":
            qs = urllib.parse.parse_qs(parsed.query)
            name = re.sub(r'[\\/:*?"<>|]+', "_", Path(qs.get("name", ["upload.bin"])[0]).name) or "upload.bin"
            sub = re.sub(r"[^A-Za-z0-9_.-]+", "_", qs.get("dir", [""])[0]).strip("._")
            target = store.path("uploads", *([sub] if sub else []), name)
            k = 1
            while target.exists():  # never overwrite an earlier upload
                target = target.with_name(f"{Path(name).stem}_{k}{Path(name).suffix}")
                k += 1
            remaining = int(self.headers.get("Content-Length") or 0)
            with open(target, "wb") as fh:  # stream: image packs can be large
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            return self._send_json({"ok": True, "path": store.rel(target)})
        self._send_error(404, "not found")


def serve(job_dir: str | Path, port: int = 0, open_browser: bool = True) -> None:
    store = JobStore(job_dir)
    store.load()
    handler = type("BoundHandler", (Handler,), {"store": store})
    httpd = None
    for candidate in ([port] if port else []) + list(range(8765, 8800)):
        try:
            httpd = _QuietServer(("127.0.0.1", candidate), handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError("找不到可用端口")
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    lock = lock_path(store.dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    write_json(lock, {"url": url, "port": httpd.server_address[1], "pid": os.getpid(), "started_at": time.time(),
                      "project": store.dir.name})
    print(f"KARAOKE_MASTER_URL {url}", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    finally:
        info = read_json(lock)
        if info and info.get("pid") == os.getpid():
            lock.unlink(missing_ok=True)
        httpd.server_close()
