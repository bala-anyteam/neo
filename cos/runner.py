"""Launch Claude Code or Codex headless and record every event to disk.

Layout per run (under COS_HOME/runs/<run_id>/):
    meta.json     engine, prompt, cwd, mode, status, session_id, cost, timestamps
    events.jsonl  normalized timeline events (what the dashboard renders)
    raw.jsonl     untouched engine output lines, for debugging the normalizer
    stderr.log    engine stderr
"""

from __future__ import annotations

import json
import hashlib
import fcntl
import math
import sys
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import conclusion
from .events import normalize

ENGINES = ("claude", "codex")
MODES = ("readonly", "edit", "full")
TERMINAL = ("done", "failed", "cancelled")
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
MAX_PROMPT = 100_000

# A turn that dies on a passing Claude/Codex API fault (dropped stream, overload, 5xx) is resumed
# with "continue" instead of being left for a person to nudge. Auth, quota and prompt errors are
# not transient and are left alone.
AUTO_RESTART_PROMPT = "continue"
AUTO_RESTART_DELAYS = (15.0, 60.0, 180.0)  # seconds before each attempt; its length is the cap
# A live session's streak resets when the agent makes progress; this caps restarts between two
# human prompts all the same, so an agent that always fails at the same point cannot loop forever.
MAX_AUTO_RESTARTS_PER_PROMPT = 6
# A successful process exit is not task completion when the agent says it is waiting.
# Keep this separate from API retries, scoped to briefed Slack agents, and bounded.
WAITING_CONTINUATION_DELAYS = (15.0, 60.0, 180.0)
WAITING_CONTINUATION_PROMPT = (
    "Your last turn ended with Status: waiting, but the requested task is unfinished. "
    "Continue the already-authorized task from the recorded state. Inspect current checks and rollout "
    "before acting; do not repeat a completed merge, release or deployment. Keep waiting/polling with "
    "tools while work can proceed. Preserve all human approval gates, including stage-video review. "
    "If a person must act, stop with Status: needs-you and name the person and exact action. "
    "If you cannot proceed, use Status: blocked with the concrete reason. Do not expand scope."
)
_TRANSIENT_ERROR_RE = re.compile(
    r"connection lost|stopped arriving|overloaded|\b5\d\d\b|internal server error|bad gateway"
    r"|service unavailable|timed? ?out|econnreset|econnrefused|socket hang up|fetch failed"
    r"|network error|stream (disconnected|error|closed)|terminated",
    re.IGNORECASE,
)
_PERMANENT_ERROR_RE = re.compile(
    r"login|authenticat|oauth|credit balance|usage limit|hit your limit|quota|prompt is too long"
    r"|invalid.?request|\b40[0-4]\b",
    re.IGNORECASE,
)


def is_transient_engine_error(text: Optional[str]) -> bool:
    """True for a Claude/Codex API error that a retry can clear."""
    text = text or ""
    return bool(_TRANSIENT_ERROR_RE.search(text)) and not _PERMANENT_ERROR_RE.search(text)

CLAUDE_MODE_FLAGS = {
    # Headless default mode auto-denies anything that needs approval; denials show in the timeline.
    "readonly": ["--permission-mode", "default"],
    "edit": ["--permission-mode", "acceptEdits"],
    "full": ["--permission-mode", "bypassPermissions"],
}
CODEX_SANDBOX = {"readonly": "read-only", "edit": "workspace-write", "full": "danger-full-access"}


def cos_home() -> Path:
    return Path(os.environ.get("COS_HOME", Path(__file__).resolve().parent.parent)).resolve()


def runs_dir() -> Path:
    path = cos_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(run_id or ""))


def cli_path(engine: str) -> str:
    """Explicit paths matter under launchd, whose PATH differs from an interactive shell."""
    return os.environ.get(f"{engine.upper()}_CLI_PATH") or shutil.which(engine) or engine


def _slack_target(meta: dict) -> Optional[tuple[str, str]]:
    channel, thread = meta.get("slack_channel"), meta.get("slack_thread")
    return (str(channel), str(thread)) if channel and thread else None


def run_context(meta: dict) -> Optional[str]:
    """Where this run answers. Agents register deploy watches with the deploy's slack_channel and
    slack_thread; never told its thread, an agent wrote null and deploy-watch refused the watch."""
    target = _slack_target(meta)
    if not target:
        return None
    return (f"This run answers in Slack channel {target[0]}, thread_ts {target[1]} (also $NEO_SLACK_CHANNEL and "
            "$NEO_SLACK_THREAD). Use exactly these as slack_channel and slack_thread for any deploy-watch "
            "entry you register.")


def engine_env(meta: dict) -> dict:
    """The bot's environment plus this run's id and Slack thread (none inherited from the bot)."""
    env = {k: v for k, v in os.environ.items() if k not in ("NEO_SLACK_CHANNEL", "NEO_SLACK_THREAD")}
    env["NEO_RUN_ID"] = meta["id"]
    env["COS_HOME"] = str(cos_home())
    env["NEO_TASKS_CLI"] = str(Path(__file__).with_name("tasks.py"))
    env["NEO_TASKS_PYTHON"] = sys.executable
    target = _slack_target(meta)
    if target:
        env["NEO_SLACK_CHANNEL"], env["NEO_SLACK_THREAD"] = target
    return env


def build_command(
    engine: str, prompt: str, mode: str, session_id: Optional[str] = None, system: Optional[str] = None,
    extra_args: Optional[list[str]] = None, conclude: bool = False,
) -> list[str]:
    """system: standing instructions. Claude takes them natively (--append-system-prompt, on every
    turn); Codex has no equivalent flag, so they lead the first prompt of the session.
    extra_args: more CLI options for the engine (for example Claude's --allowedTools, --json-schema).
    conclude: an agent session (it has the agent brief): Claude gets the Stop hook that makes each turn end
    with a Status line (cos/conclusion.py). Scheduled jobs leave it off."""
    extra_args = list(extra_args or [])
    model = os.environ.get(f"{engine.upper()}_MODEL")
    if engine == "claude":
        cmd = [cli_path("claude"), "-p", "--output-format", "stream-json", "--verbose"]
        cmd += CLAUDE_MODE_FLAGS[mode]
        if conclude:
            cmd += ["--settings", conclusion.settings_json()]
        if system:
            cmd += ["--append-system-prompt", system]
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd + extra_args + ["--", prompt]  # "--": a prompt starting with "-" is not an option
    if engine == "codex":
        if system and not session_id:
            prompt = f"{system}\n\n---\n\n{prompt}"
        if mode == "full":
            access = ["--dangerously-bypass-approvals-and-sandbox"]
        elif session_id:
            access = ["-c", f'sandbox_mode="{CODEX_SANDBOX[mode]}"']
        else:
            access = ["-s", CODEX_SANDBOX[mode]]
        cmd = [cli_path("codex"), "exec"]
        if session_id:
            cmd += ["resume"]
        cmd += ["--json", "--skip-git-repo-check", *access]
        if model:
            cmd += ["--model", model]
        cmd += extra_args
        cmd += ["--"]  # a prompt (or id) starting with "-" is not an option
        if session_id:
            cmd += [session_id]
        return cmd + [prompt]
    raise ValueError(f"unknown engine: {engine}")


def validate_request(engine: str, prompt: str, cwd: str, mode: str) -> Optional[str]:
    if engine not in ENGINES:
        return f"engine must be one of {ENGINES}"
    if mode not in MODES:
        return f"mode must be one of {MODES}"
    if not prompt or not prompt.strip():
        return "prompt is required"
    if len(prompt) > MAX_PROMPT:
        return f"prompt is longer than {MAX_PROMPT} characters"
    if not cwd or not Path(cwd).expanduser().is_dir():
        return f"cwd is not a directory: {cwd}"
    return None


class _ProcessRunLock:
    """Reentrant thread lock plus a per-run flock shared by bot, dashboard and CLI."""
    def __init__(self, path):
        self.path, self.thread_lock, self.local = path, threading.RLock(), threading.local()

    def __enter__(self):
        self.thread_lock.acquire()
        try:
            depth = getattr(self.local, "depth", 0)
            if depth == 0:
                self.local.handle = self.path.open("a")
                fcntl.flock(self.local.handle, fcntl.LOCK_EX)
            self.local.depth = depth + 1
            return self
        except BaseException:
            self.thread_lock.release()
            raise

    def __exit__(self, *args):
        self.local.depth -= 1
        if self.local.depth == 0:
            fcntl.flock(self.local.handle, fcntl.LOCK_UN)
            self.local.handle.close()
        self.thread_lock.release()


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _run_lock(run_id):
    path = runs_dir() / run_id / ".state.lock"
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), _ProcessRunLock(path))


_WAKES: dict[str, threading.Event] = {}


def _wake_event(run_id: str) -> threading.Event:
    """Set by cancel() to cut an auto-restart back-off short, from any Run object in this process."""
    with _LOCKS_GUARD:
        return _WAKES.setdefault(run_id, threading.Event())


class Run:
    """One conversation with one engine; each prompt or follow-up is a turn."""

    def __init__(self, run_id: str):
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id}")
        self.id = run_id
        self.dir = runs_dir() / run_id
        self._lock = _run_lock(run_id)
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._last_error: Optional[str] = None  # the engine's error text for the latest turn
        self._turn_answered = False  # the latest turn already produced a successful result
        self._stderr_offset = 0

    # ---------------------------------------------------------------- storage

    @classmethod
    def create(
        cls,
        engine: str,
        prompt: str,
        cwd: str,
        mode: str,
        source: str = "cli",
        runtime: str = "headless",
        extra: Optional[dict] = None,
    ) -> "Run":
        """runtime "headless" runs are executed by this process; "herdr" runs are
        interactive agents in herdr that a watcher records (see cos.interactive)."""
        if runtime == "herdr":
            mode = "interactive"
            error = validate_request(engine, prompt, cwd, "full")
        else:
            error = validate_request(engine, prompt, cwd, mode)
        if error:
            raise ValueError(error)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        run = cls(run_id)
        run.dir.mkdir(parents=True)
        run.write_meta(
            {
                "id": run_id,
                "engine": engine,
                "prompt": prompt,
                "cwd": str(Path(cwd).expanduser().resolve()),
                "mode": mode,
                "runtime": runtime,
                "source": source,
                **(extra or {}),
                "status": "queued",
                "session_id": None,
                "turns": 0,
                "cost_usd": 0.0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "pid": None,
            }
        )
        (run.dir / "events.jsonl").touch()
        return run

    def meta(self) -> dict:
        return json.loads((self.dir / "meta.json").read_text())

    def write_meta(self, meta: dict) -> None:
        meta["updated_at"] = now_iso()
        # Unique temp name: several processes (bot, dashboard, CLI) may write one run.
        tmp = self.dir / f"meta.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(self.dir / "meta.json")

    def update_meta(self, **fields) -> dict:
        with self._lock:
            meta = self.meta()
            meta.update(fields)
            self.write_meta(meta)
            return meta

    def _next_seq(self) -> int:
        path = self.dir / "events.jsonl"
        with path.open("rb") as fh:
            return sum(1 for _ in fh)

    def append_event(self, event: dict, turn: int) -> dict:
        with self._lock:
            event = {"seq": self._next_seq(), "ts": now_iso(), "turn": turn, **event}
            with (self.dir / "events.jsonl").open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        return event

    # -------------------------------------------------------------- execution

    def execute(self, prompt: Optional[str] = None, on_event: Optional[Callable[[dict], None]] = None) -> dict:
        final = self._execute_with_restarts(prompt, on_event)
        return self._continue_waiting(final, on_event, attempts=0)

    def _continue_waiting(self, final: dict, on_event=None, attempts: Optional[int] = None) -> dict:
        """Continue clean waiting exits without conflating an engine turn with the user's task.

        Adoption uses the persisted attempt count, so a bot restart cannot reset the cap.
        Explicit human/blocked conclusions and non-Slack jobs are never continued.
        """
        emit = on_event or (lambda _e: None)
        attempts = int(final.get("waiting_continuations") or 0) if attempts is None else attempts
        while (final.get("status") == "done" and final.get("source") == "slack"
               and final.get("conclude") and (final.get("conclusion") or {}).get("state") == "waiting"
               and not self._cancelled):
            if not final.get("session_id") or attempts >= len(WAITING_CONTINUATION_DELAYS):
                with self._lock:
                    current = self.meta()
                    if current["status"] != "done" or current["turns"] != final["turns"]:
                        return current
                    message = "Unfinished task could not be continued; inspect the trace before resuming."
                    emit(self.append_event({"kind": "error", "title": "Continuation limit reached",
                                            "body": message, "data": {"attempts": attempts}}, final["turns"]))
                    return self.update_meta(status="failed", retry_owner=None,
                                            conclusion={"state": "blocked", "line": message})
            delay = WAITING_CONTINUATION_DELAYS[attempts]
            wake = _wake_event(self.id)
            with self._lock:
                current = self.meta()
                if current["status"] != "done" or current["turns"] != final["turns"]:
                    return current  # another caller cancelled or resumed it
                wake.clear()
                self.update_meta(status="running", pid=None, retry_owner=os.getpid(),
                                 waiting_continuations=attempts + 1,
                                 continuation={"kind":"waiting", "prompt":WAITING_CONTINUATION_PROMPT,
                                               "due_at":time.time()+delay, "turn":final["turns"], "attempt":attempts+1})
            emit(self.append_event({"kind": "status", "title": "Continue unfinished task",
                                    "body": f"Resuming in {delay:.0f}s ({attempts + 1}/{len(WAITING_CONTINUATION_DELAYS)})",
                                    "data": {"waiting_continuation": attempts + 1}}, final["turns"]))
            self._wait_checkpoint(wake, delay)
            final = self._execute_with_restarts(WAITING_CONTINUATION_PROMPT, on_event, retrying=True)
            attempts += 1
        return final

    def _wait_checkpoint(self, wake, delay):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if wake.wait(min(.5, max(0, deadline-time.monotonic()))):
                break
            meta = self.meta()
            if meta.get("status") != "running" or meta.get("retry_owner") != os.getpid():
                break

    @staticmethod
    def pending_valid(meta):
        pending = meta.get("continuation")
        if (not isinstance(pending, dict) or meta.get("status") != "running"
                or meta.get("pid") or pending.get("turn") != meta.get("turns")
                or pending.get("kind") not in ("waiting", "api")):
            return False
        limit = len(WAITING_CONTINUATION_DELAYS) if pending["kind"] == "waiting" else len(AUTO_RESTART_DELAYS)
        if (type(pending.get("attempt")) is not int or not 1 <= pending["attempt"] <= limit
                or not isinstance(pending.get("due_at"), (float,int)) or not math.isfinite(pending["due_at"])):
            return False
        if pending["kind"] == "waiting":
            return bool(meta.get("source") == "slack" and meta.get("conclude") and meta.get("session_id")
                        and isinstance(pending.get("prompt"), str))
        return pending.get("prompt") is None or isinstance(pending.get("prompt"), str)

    def _resume_pending(self, on_event=None):
        """Claim only an explicit durable wait left by a dead owner, once across processes."""
        emit = on_event or (lambda _e: None)
        with self._lock:
            meta = self.meta()
            pending = meta.get("continuation")
            if not self.pending_valid(meta) or pid_alive(meta.get("retry_owner")):
                return None
            from . import tasks
            try:
                gate = tasks.show(self.id).get("block")
            except (OSError,ValueError):
                gate = "task contract is unreadable; inspect it before resuming"
            if gate:
                final = self.update_meta(status="done",retry_owner=None,continuation=None,
                                         conclusion={"state":"needs-you","line":gate})
                emit(self.append_event({"kind":"result","title":"Human approval required",
                                        "body":"Status: needs-you — " + gate,"data":{}},meta["turns"]))
                return final
            self.update_meta(retry_owner=os.getpid(), api_retry_offset=pending.get("attempt", 0)
                             if pending["kind"] == "api" else 0)
        emit(self.append_event({"kind":"status", "title":"Recovered continuation",
                                "body":"Resuming the existing task after a runner restart",
                                "data":{"continuation_recovered":True}}, meta["turns"]))
        wake = _wake_event(self.id)
        wake.clear()
        self._wait_checkpoint(wake, max(0, min(180, float(pending["due_at"])-time.time())))
        final = self._execute_with_restarts(pending.get("prompt"), on_event, retrying=True,
                                            keep_first_turn_args=bool(pending.get("keep_first_turn_args")))
        return self._continue_waiting(final, on_event)

    def _guard_task_claim(self, text, emit, turn):
        from . import tasks
        state = (conclusion.status_of(text) or {}).get("state")
        if state in ("needs-you", "blocked", "failed") or not (self.dir / "task.json").exists():
            return text
        try:
            task = tasks.show(self.id)
            if task.get("block"):
                return "Task awaits the recorded approval.\nStatus: needs-you — " + task["block"]
            if task.get("status") == "completed":
                return text
            if state == "waiting":
                return text
            unmet = tasks.unmet(self.id)
        except (OSError, ValueError):
            unmet = ["task evidence could not be read; inspect the recorded contract"]
        self.update_meta(task_contract=True)
        if not unmet:
            if tasks.complete(self.id, task.get("revision")):
                return text
            unmet = ["task contract changed before completion could be recorded"]
        emit(self.append_event({"kind":"status", "title":"Completion evidence missing",
                                "body":"; ".join(unmet)[:1000],
                                "data":{"completion_refused":True,"unmet":unmet}}, turn))
        return ("The recorded task still has unmet outcomes: " + "; ".join(unmet)[:1000]
                + "\nStatus: waiting — continuing the recorded task; completion evidence is missing.")

    def _execute_with_restarts(self, prompt: Optional[str] = None,
                               on_event: Optional[Callable[[dict], None]] = None, retrying: bool = False,
                               keep_first_turn_args: bool = False) -> dict:
        """Run one turn to completion. Without a prompt, runs the initial prompt. A turn that ends
        on a transient engine error is resumed with "continue" (see AUTO_RESTART_DELAYS).

        While it waits the run stays "running" with no engine pid and retry_owner set to this
        process: callers keep treating it as busy, and if this process dies, fresh_meta() sees a
        dead owner and closes the run out as it does for any engine lost in a restart."""
        final = self._execute_turn(prompt, on_event, retrying=retrying, keep_first_turn_args=keep_first_turn_args)
        emit = on_event or (lambda _e: None)
        first_turn = prompt is None or keep_first_turn_args
        wake = _wake_event(self.id)
        retry_offset = int(self.meta().get("api_retry_offset") or 0) if retrying else 0
        for attempt, delay in enumerate((*AUTO_RESTART_DELAYS[retry_offset:], None), retry_offset + 1):
            if (final.get("status") != "failed" or self._cancelled or self._turn_answered
                    or not is_transient_engine_error(self._last_error)):
                break
            turn = final.get("turns") or 1
            if delay is None:
                emit(self.append_event({
                    "kind": "error", "title": "Auto-restart gave up",
                    "body": f"Still failing after {len(AUTO_RESTART_DELAYS)} restarts: {(self._last_error or '')[:500]}",
                    "data": {"auto_restart": "exhausted"},
                }, turn))
                break
            with self._lock:
                if self.meta()["status"] != "failed":
                    break  # someone else moved the run on (a dashboard message, a cancel)
                wake.clear()
                retry_prompt = AUTO_RESTART_PROMPT if final.get("session_id") else prompt
                self.update_meta(status="running", pid=None, retry_owner=os.getpid(),
                                 continuation={"kind":"api", "prompt":retry_prompt, "due_at":time.time()+delay,
                                               "turn":final["turns"], "attempt":attempt,
                                               "keep_first_turn_args":first_turn})
            emit(self.append_event({
                "kind": "status", "title": "Auto-restart",
                "body": f"{final['engine']} API error; retrying in {delay:.0f}s ({attempt}/{len(AUTO_RESTART_DELAYS)})",
                "data": {"auto_restart": attempt, "error": (self._last_error or "")[:500]},
            }, turn))
            self._wait_checkpoint(wake, delay)
            if final.get("session_id"):
                final = self._execute_turn(AUTO_RESTART_PROMPT, on_event, keep_first_turn_args=first_turn, retrying=True)
            else:  # died before a session existed: run the same turn again
                final = self._execute_turn(prompt, on_event, retrying=True)
            if final.get("status") == "cancelled":
                break
        return final

    def _execute_turn(self, prompt: Optional[str], on_event: Optional[Callable[[dict], None]],
                      keep_first_turn_args: bool = False, retrying: bool = False) -> dict:
        with self._lock:  # claim the run so two callers cannot start turns at once
            meta = self.meta()
            if meta.get("runtime", "headless") != "headless":
                raise RuntimeError("live runs are driven through herdr, not execute()")
            if retrying and (meta["status"] != "running" or meta.get("retry_owner") != os.getpid()):
                return meta  # cancelled (or taken over) while we waited
            if meta["status"] == "running" and not retrying:
                raise RuntimeError("run is already executing a turn")
            if retrying and (self.dir / "task.json").exists():
                from . import tasks
                try:
                    gate = tasks.show(self.id).get("block")
                except (OSError,ValueError):
                    return self.update_meta(status="failed",pid=None,retry_owner=None,continuation=None,
                                            conclusion={"state":"blocked","line":"Task contract is unreadable"})
                if gate:
                    final = self.update_meta(status="done",pid=None,retry_owner=None,continuation=None,
                                             conclusion={"state":"needs-you","line":gate})
                    if on_event:
                        on_event(self.append_event({"kind":"result","title":"Human approval required",
                                                    "body":"Status: needs-you — " + gate,"data":{}},meta["turns"]))
                    return final
            is_follow_up = prompt is not None
            if is_follow_up and not meta.get("session_id"):
                raise RuntimeError("no session id recorded yet; cannot send a follow-up")
            turn = meta["turns"] + 1
            self._cancelled = False
            self._last_error, self._turn_answered = None, False
            self.write_meta({**meta, "status": "running", "turns": turn, "pid": None, "retry_owner": None,
                             "conclusion": None, "continuation": None,
                             "api_retry_offset": meta.get("api_retry_offset",0) if retrying else 0})

        engine, mode, cwd = meta["engine"], meta["mode"], meta["cwd"]
        text = prompt if is_follow_up else meta["prompt"]
        emit = on_event or (lambda _e: None)
        try:
            system = "\n\n".join(filter(None, (meta.get("system_prompt"), run_context(meta)))) or None
            brief_digest = hashlib.sha256((system or "").encode()).hexdigest()
            command_text = text
            if engine == "codex" and is_follow_up and system and meta.get("brief_digest") != brief_digest:
                command_text = f"Updated standing instructions and owner memory:\n{system}\n\n---\n\n{text}"
            cmd = build_command(engine, command_text, mode, meta["session_id"] if is_follow_up else None,
                                system=system, conclude=bool(meta.get("conclude")),
                                extra_args=(meta.get("cli_args") or [])
                                + (meta.get("first_turn_args") or [] if keep_first_turn_args or not is_follow_up else []))
            emit(self.append_event({"kind": "prompt", "title": "You", "body": text, "data": {}}, turn))
            raw_path = self.dir / "raw.jsonl"
            offset = raw_path.stat().st_size if raw_path.exists() else 0
            stderr_path = self.dir / "stderr.log"
            self._stderr_offset = stderr_path.stat().st_size if stderr_path.exists() else 0
            # The engine writes straight to files, not to a pipe into this process: if the bot
            # restarts, the engine keeps running and a new bot picks up where this one stopped.
            with raw_path.open("ab") as out, (self.dir / "stderr.log").open("ab") as err:
                self._proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                              start_new_session=True, env=engine_env(meta))
        except FileNotFoundError as exc:
            emit(self.append_event({"kind": "error", "title": "Engine not found", "body": str(exc), "data": {}}, turn))
            return self.update_meta(status="failed", pid=None)
        except Exception:
            self.update_meta(status="failed", pid=None)
            raise
        self.update_meta(pid=self._proc.pid, raw_offset=offset, cost_before_turn=meta.get("cost_usd") or 0.0,
                         turn_started_at=time.time())
        if self._cancelled:  # stop requested while we were starting
            self._kill(self._proc.pid)
        final = self._follow(emit, turn, engine, offset, meta.get("cost_usd") or 0.0, time.time(), proc=self._proc)
        if final.get("status") == "done":
            self.update_meta(brief_digest=brief_digest)
        return final

    def _follow(self, emit, turn: int, engine: str, offset: int, cost: float, started: float,
                proc: Optional[subprocess.Popen] = None, pid: Optional[int] = None) -> dict:
        """Read the engine's output file as it grows until the engine exits, then record the outcome.
        Works for an engine we started (proc) and for one a previous bot process started (pid)."""
        raw_path = self.dir / "raw.jsonl"
        last_text, saw_result, completed, failed_turn = "", False, False, False
        self._last_error = None
        status, code = "failed", None
        buffered = b""
        last_save = 0.0
        try:
            while True:
                alive = (proc.poll() is None) if proc else pid_alive(pid)
                with raw_path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                if chunk:
                    buffered += chunk
                    offset += len(chunk)
                    *lines, buffered = buffered.split(b"\n")
                    for raw_line in lines:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            emit(self.append_event({"kind": "debug", "title": "non-JSON output", "body": line, "data": {}}, turn))
                            continue
                        if not isinstance(raw, dict):
                            continue
                        rtype = raw.get("type")
                        if rtype == "rate_limit_event":
                            save_rate_limits(raw.get("rate_limit_info") or {})
                        completed = completed or rtype in ("result", "turn.completed")
                        failed_turn = failed_turn or rtype == "turn.failed" or (rtype == "result" and raw.get("is_error"))
                        if rtype == "result" and raw.get("is_error"):
                            self._last_error = str(raw.get("result") or raw.get("subtype") or "")
                        elif rtype in ("result", "turn.completed"):
                            self._turn_answered = True
                        elif rtype in ("turn.failed", "error"):
                            self._last_error = json.dumps(raw.get("error") or raw.get("message") or raw)
                        for event in normalize(engine, raw):
                            data = event.get("data") or {}
                            if event["kind"] == "session" and data.get("session_id"):
                                self.update_meta(session_id=data["session_id"])
                            if engine == "claude" and event["kind"] == "result":
                                event["body"] = self._guard_task_claim(event["body"], emit, turn)
                            if event["kind"] == "text" or (engine == "claude" and event["kind"] == "result"):
                                last_text = event["body"]
                            if event["kind"] in ("result", "error") and "cost_usd" in data:
                                saw_result = True
                                cost += data.get("cost_usd") or 0.0
                            emit(self.append_event(event, turn))
                    if time.monotonic() - last_save > 1:
                        last_save = time.monotonic()
                        self.update_meta(raw_offset=offset - len(buffered))
                if not alive and not chunk:
                    break
                if not chunk:
                    time.sleep(0.15)

            if proc is not None:
                code = proc.wait()
            else:  # not our child: judge by what the engine wrote
                code = 0 if completed and not failed_turn else 1
            if engine == "codex" and not saw_result and code == 0:
                last_text = self._guard_task_claim(last_text, emit, turn)
                emit(self.append_event({"kind": "result", "title": "Final result", "body": last_text, "data": {}}, turn))

            if self._cancelled:
                status = "cancelled"
                emit(self.append_event({"kind": "error", "title": "Cancelled", "body": "Stopped by user", "data": {}}, turn))
            elif code != 0:
                tail = (self.dir / "stderr.log").read_text(errors="replace")[-2000:]
                if self._last_error is None and proc is not None:  # this turn's stderr only
                    with (self.dir / "stderr.log").open("rb") as fh:
                        fh.seek(self._stderr_offset)
                        self._last_error = fh.read().decode("utf-8", errors="replace")[-2000:]
                emit(self.append_event({"kind": "error", "title": f"Engine exited {code}", "body": tail, "data": {"exit_code": code}}, turn))
            else:
                status = "done"
        finally:
            if proc is not None:
                self._proc = None
                if proc.poll() is None:  # leaving early (an exception): do not orphan the engine
                    self._kill(proc.pid)
                    proc.wait()
            final = self.update_meta(
                status=status,
                pid=None,
                cost_usd=round(cost, 4),
                raw_offset=offset,
                last_turn_seconds=round(time.time() - started, 1),
                # The agent's own Status line for this turn (cos/conclusion.py); home sorts the run by it.
                conclusion=conclusion.status_of(last_text) if status == "done" else None,
            )
        return final

    def adopt(self, on_event: Optional[Callable[[dict], None]] = None) -> Optional[dict]:
        """Keep following a turn a previous bot process started, if its engine is still running.
        Returns the final meta, or None when there is nothing to adopt."""
        meta = self.meta()
        if meta.get("runtime", "headless") != "headless" or meta["status"] != "running":
            return None
        if not pid_alive(meta.get("pid")):
            return self._resume_pending(on_event)
        final = self._follow(on_event or (lambda _e: None), meta.get("turns") or 1, meta["engine"],
                            int(meta.get("raw_offset") or 0), float(meta.get("cost_before_turn") or 0.0),
                            float(meta.get("turn_started_at") or time.time()), pid=meta["pid"])
        return self._continue_waiting(final, on_event)

    @staticmethod
    def _kill(pid: int) -> bool:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def cancel(self) -> bool:
        """Stop the running turn, whether it was started by this process or another."""
        self._cancelled = True
        proc = self._proc
        with self._lock:
            meta = self.meta()
            pid = proc.pid if proc else meta.get("pid")
            if not pid and meta.get("status") == "running" and meta.get("retry_owner"):
                # Waiting to auto-restart: don't, and wake the waiter so it gives up now.
                self.update_meta(status="cancelled", retry_owner=None, continuation=None)
                _wake_event(self.id).set()
                return True
        if not pid:
            return False
        if not self._kill(pid):
            return False
        if proc is None:
            # Another process owns the run; record the outcome ourselves.
            self.update_meta(status="cancelled", pid=None, continuation=None)
        return True


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        # If it is our own child that has exited, reap it: a zombie still answers kill(0).
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass  # not our child (for example, started by a previous bot process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def save_rate_limits(info: dict) -> None:
    """Claude reports its usage windows (5-hour, weekly) in the stream; keep the latest."""
    windows = info.get("unifiedWindows") or {}
    record = {"at": now_iso(), "status": info.get("status"), "windows": {
        name: {"used": w.get("utilization"), "resets_at": w.get("resetsAt")} for name, w in windows.items() if isinstance(w, dict)}}
    tmp = cos_home() / "usage.json.tmp"
    try:
        tmp.write_text(json.dumps(record))
        tmp.replace(cos_home() / "usage.json")
    except OSError:
        pass


def usage_summary() -> dict:
    """Spend today and over the last 7 days (from run records), plus Claude's latest limits."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now.timestamp() - 7 * 86400)
    spent_today = spent_week = 0.0
    runs_today = 0
    by_kind: dict[str, float] = {}
    for meta in list_runs(2000):
        try:
            created = datetime.fromisoformat(meta["created_at"])
        except (KeyError, ValueError):
            continue
        if created.timestamp() < week_start:
            break
        cost = float(meta.get("cost_usd") or 0)
        spent_week += cost
        kind = meta.get("kind") or meta.get("source") or "other"
        by_kind[kind] = by_kind.get(kind, 0) + cost
        if meta["created_at"][:10] == today:
            spent_today += cost
            runs_today += 1
    try:
        limits = json.loads((cos_home() / "usage.json").read_text())
    except (OSError, json.JSONDecodeError):
        limits = None
    return {"today_usd": round(spent_today, 2), "week_usd": round(spent_week, 2), "runs_today": runs_today,
            "week_by_kind": {k: round(v, 2) for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])}, "claude_limits": limits}


def list_runs(limit: int = 100, include_running: bool = False) -> list[dict]:
    """The newest `limit` runs by creation. include_running also keeps every older run that is still
    running: a Slack thread resumes its first run for each follow-up, so a live turn can belong to a
    run created days and hundreds of runs ago."""
    runs = []
    for path in sorted(runs_dir().iterdir(), reverse=True):
        if len(runs) >= limit and not include_running:
            break
        if not (path / "meta.json").exists() or not valid_run_id(path.name):
            continue
        try:
            meta = json.loads((path / "meta.json").read_text())
        except json.JSONDecodeError:
            continue
        if len(runs) < limit or meta.get("status") == "running":
            runs.append(meta)
    return runs
