"""Job directory state shared by the agent, the CLI tools and the web server.

Layout of a job directory::

    job.json        master state (stage, song info, lyrics, options, progress...)
    inbox.jsonl     user actions posted from the web page (agent consumes)
    inbox.cursor    number of inbox lines already consumed by the agent
    chat.jsonl      conversation shown in the web page (user prompts + agent notes)
    drafts/         web page drafts (autosaved form state per stage)
    media/ lyrics/ analysis/ timing/ previews/ render/ export/ logs/

All writers go through :class:`JobStore`, which serialises read-modify-write
cycles with a lock file so a background stage runner, the agent and the web
server never clobber each other.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA = 1

STAGE_TITLES = {1: "素材与确认", 2: "自动打轴", 3: "审阅与导出"}

STAGE2_STEPS = [
    ("prepare", "准备音频"),
    ("separate", "人声分离"),
    ("pronounce", "注音与对齐单元"),
    ("align", "强制对齐"),
    ("refine", "尾音与行首修正"),
    ("qa", "Agent 质检"),
    ("project", "生成工程"),
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: a reader may hold the file open for an instant.
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.05 * (attempt + 1))
    return json.loads(path.read_text(encoding="utf-8"))


def new_job_state(job_id: str, inputs: dict) -> dict:
    return {
        "schema": SCHEMA,
        "id": job_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "stage": 1,
        "status": "working",
        "status_text": "已创建任务",
        "inputs": inputs,
        "media": {},
        "song": {"singers": []},
        "segment": None,
        "lyrics": {"candidates": [], "selected": None, "lines": []},
        "options": {},
        "previews": {},
        "progress": {
            "steps": [
                {"id": sid, "label": label, "state": "pending", "progress": 0.0}
                for sid, label in STAGE2_STEPS
            ]
        },
        "timing": {},
        "exports": [],
        "agent": {"waiting_for": None},
    }


class JobStore:
    def __init__(self, job_dir: str | Path):
        self.dir = Path(job_dir).resolve()
        self.state_path = self.dir / "job.json"
        self.lock_path = self.dir / ".job.lock"

    # ------------------------------------------------------------------ paths
    def path(self, *parts: str) -> Path:
        p = self.dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def rel(self, path: str | Path) -> str:
        """Project-relative path; files outside the project (e.g. an output
        folder next to the user's media) stay absolute."""
        p = Path(path).resolve()
        try:
            return p.relative_to(self.dir).as_posix()
        except ValueError:
            return p.as_posix()

    def abs(self, rel: str) -> Path:
        return (self.dir / rel).resolve()

    # ------------------------------------------------------------------ lock
    @contextlib.contextmanager
    def lock(self, timeout: float = 30.0) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 15:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"job lock busy: {self.lock_path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()

    # ------------------------------------------------------------------ state
    def exists(self) -> bool:
        return self.state_path.is_file()

    def create(self, job_id: str, inputs: dict) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in ("media", "lyrics", "analysis", "timing", "previews", "render", "export", "logs", "drafts"):
            (self.dir / sub).mkdir(exist_ok=True)
        state = new_job_state(job_id, inputs)
        write_json(self.state_path, state)
        return state

    def load(self) -> dict:
        state = read_json(self.state_path)
        if state is None:
            raise FileNotFoundError(f"不是任务目录（缺少 job.json）：{self.dir}")
        return state

    def update(self, fn: Callable[[dict], Any]) -> dict:
        with self.lock():
            state = self.load()
            fn(state)
            state["updated_at"] = now_iso()
            write_json(self.state_path, state)
            return copy.deepcopy(state)

    def patch(self, **fields: Any) -> dict:
        def apply(state: dict) -> None:
            for key, value in fields.items():
                state[key] = value

        return self.update(apply)

    def set_status(self, text: str, status: str | None = None, stage: int | None = None) -> None:
        def apply(state: dict) -> None:
            state["status_text"] = text
            if status:
                state["status"] = status
            if stage:
                state["stage"] = stage

        self.update(apply)
        self.log(text)

    # ------------------------------------------------------------- progress
    def step(self, step_id: str, *, state: str | None = None, progress: float | None = None,
             detail: str | None = None, eta: float | None = None, error: str | None = None) -> None:
        def apply(st: dict) -> None:
            steps = st.setdefault("progress", {}).setdefault("steps", [])
            for item in steps:
                if item["id"] == step_id:
                    break
            else:
                item = {"id": step_id, "label": step_id, "state": "pending", "progress": 0.0}
                steps.append(item)
            if state is not None:
                item["state"] = state
                if state == "running" and "started_at" not in item:
                    item["started_at"] = time.time()
                if state == "done":
                    item["progress"] = 1.0
                    item["finished_at"] = time.time()
                    item.pop("eta", None)
            if progress is not None:
                item["progress"] = max(0.0, min(1.0, float(progress)))
            if detail is not None:
                item["detail"] = detail
            if eta is not None:
                item["eta"] = round(float(eta), 1)
            if error is not None:
                item["error"] = error

        self.update(apply)

    def reset_steps(self) -> None:
        def apply(st: dict) -> None:
            for item in st.get("progress", {}).get("steps", []):
                for key in ("started_at", "finished_at", "eta", "error", "detail"):
                    item.pop(key, None)
                item["state"] = "pending"
                item["progress"] = 0.0

        self.update(apply)

    # ------------------------------------------------------------------ logs
    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"
        with open(self.path("logs", "events.log"), "a", encoding="utf-8") as fh:
            fh.write(line)

    # ------------------------------------------------------------------ chat
    def chat(self, role: str, text: str, **extra: Any) -> dict:
        entry = {"id": uuid.uuid4().hex[:10], "time": now_iso(), "role": role, "text": text, **extra}
        with open(self.path("chat.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def chat_history(self) -> list[dict]:
        return _read_jsonl(self.dir / "chat.jsonl")

    # ----------------------------------------------------------------- inbox
    def post_action(self, action: dict) -> dict:
        entry = {"id": uuid.uuid4().hex[:10], "time": now_iso(), **action}
        with open(self.path("inbox.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def pending_actions(self) -> list[dict]:
        items = _read_jsonl(self.dir / "inbox.jsonl")
        cursor = self._cursor()
        return items[cursor:]

    def consume_actions(self) -> list[dict]:
        with self.lock():
            items = _read_jsonl(self.dir / "inbox.jsonl")
            cursor = self._cursor()
            fresh = items[cursor:]
            (self.dir / "inbox.cursor").write_text(str(len(items)), encoding="utf-8")
        return fresh

    def _cursor(self) -> int:
        try:
            return int((self.dir / "inbox.cursor").read_text(encoding="utf-8").strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0

    # ---------------------------------------------------------------- drafts
    def save_draft(self, name: str, data: Any) -> None:
        write_json(self.path("drafts", f"{name}.json"), data)

    def load_draft(self, name: str) -> Any:
        return read_json(self.dir / "drafts" / f"{name}.json")


def _read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
