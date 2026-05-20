"""
Persistent Claude Code process in a PTY.

Persistent PTY session. The claude process stays alive across
send_message calls — no cold start, instant response.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pty
import re
import select
import threading
import time
from enum import Enum, auto

logger = logging.getLogger(__name__)

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>=]|\x1b[\(\[].*?[\x40-\x7e]')
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')  # keep \n \t


class PTYState(Enum):
    STARTING = auto()
    IDLE = auto()
    BUSY = auto()
    ERROR = auto()


class PTYSession:
    def __init__(self, cwd: str | None = None):
        self.cwd = cwd or os.getcwd()
        self.state = PTYState.STARTING
        self._master_fd: int | None = None
        self._pid: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._buffer: list[bytes] = []
        self._buffer_lock = threading.Lock()
        self._turn_output = ""
        self._turn_complete = threading.Event()
        self._start_time = 0.0
        self._output_callback: callable | None = None
        self.session_id: str = ""

    def set_output_callback(self, cb: callable) -> None:
        """Callback(bytes) called on every PTY output chunk."""
        self._output_callback = cb

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        master, slave = pty.openpty()
        pid = os.fork()
        if pid == 0:
            os.close(master)
            os.setsid()
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            try:
                os.chdir(self.cwd)
            except OSError:
                pass
            os.execvp("claude", ["claude"])
            os._exit(1)

        self._master_fd = master
        self._pid = pid
        os.close(slave)
        self._running = True
        self.state = PTYState.STARTING
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        logger.info("PTY session started, pid=%d cwd=%s", pid, self.cwd)

    def stop(self) -> None:
        self._running = False
        if self._pid:
            try:
                os.kill(self._pid, 9)
            except OSError:
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        logger.info("PTY session stopped")

    @property
    def alive(self) -> bool:
        if self._pid is None:
            return False
        try:
            os.kill(self._pid, 0)
            return True
        except OSError:
            return False

    # ── output reader ──────────────────────────────────────────

    def _read_loop(self) -> None:
        while self._running and self._master_fd is not None:
            try:
                r, _, _ = select.select([self._master_fd], [], [], 0.5)
                if r:
                    data = os.read(self._master_fd, 4096)
                    if not data:
                        break
                    with self._buffer_lock:
                        self._buffer.append(data)
                        if self.state == PTYState.BUSY:
                            self._turn_output += data.decode(errors="replace")
                    # Push to web terminal clients
                    if self._output_callback:
                        try:
                            self._output_callback(data)
                        except Exception:
                            pass
            except OSError:
                break

    # ── prompt I/O ─────────────────────────────────────────────

    def write_prompt(self, prompt: str) -> None:
        if self._master_fd is None:
            raise RuntimeError("PTY not started")
        self._turn_output = ""
        self._turn_complete.clear()
        self.state = PTYState.BUSY
        self._start_time = time.time()
        os.write(self._master_fd, prompt.encode("utf-8"))
        time.sleep(0.1)
        os.write(self._master_fd, b"\r")
        logger.info("Prompt written: %s", prompt[:80])

    def write_raw(self, data: bytes) -> None:
        """Write raw bytes to PTY — for web terminal keystrokes."""
        if self._master_fd is None:
            return
        os.write(self._master_fd, data)

    def mark_turn_complete(self) -> None:
        self.state = PTYState.IDLE
        self._turn_complete.set()

    def get_turn_output(self, timeout: float = 180) -> str:
        if not self._turn_complete.wait(timeout):
            self.state = PTYState.ERROR
            raise TimeoutError("Claude did not respond within timeout")
        return self._strip_ansi(self._turn_output)

    def get_recent_output(self, raw: bool = False) -> str:
        with self._buffer_lock:
            data = b"".join(self._buffer[-200:])
        text = data.decode(errors="replace")
        if raw:
            return text
        return self._strip_ansi(text)

    def get_recent_output_bytes(self) -> bytes:
        """Return raw bytes — no decode, no replacement. For xterm.js."""
        with self._buffer_lock:
            return b"".join(self._buffer[-200:])

    # ── ANSI cleaning ──────────────────────────────────────────

    @staticmethod
    def _strip_ansi(text: str) -> str:
        text = ANSI_RE.sub("", text)
        text = CTRL_RE.sub("", text)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        return "\n".join(lines)

    # ── status ─────────────────────────────────────────────────

    def status_dict(self) -> dict:
        return {
            "state": self.state.name.lower(),
            "pid": self._pid,
            "cwd": self.cwd,
            "session_id": self.session_id,
            "elapsed_s": int(time.time() - self._start_time) if self.state == PTYState.BUSY else 0,
            "output_tail": self.get_recent_output()[-500:],
        }


# ── asyncio wrapper ──────────────────────────────────────────────

class AsyncPTYSession:
    """Wrap PTYSession for use in asyncio code."""

    def __init__(self, cwd: str | None = None):
        self._session = PTYSession(cwd)
        self._turn_event = asyncio.Event()
        self._turn_output = ""
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._session.start)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._session.stop)

    @property
    def state(self) -> PTYState:
        return self._session.state

    @property
    def alive(self) -> bool:
        return self._session.alive

    @property
    def pid(self) -> int | None:
        return self._session._pid

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._session.session_id = value

    async def send_prompt(self, prompt: str, timeout: float = 180) -> str:
        """Write prompt to PTY, wait for Stop hook, return cleaned output."""
        self._turn_event.clear()
        self._turn_output = ""

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._session.write_prompt, prompt)

        try:
            await asyncio.wait_for(self._turn_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._session.state = PTYState.ERROR
            raise TimeoutError("Claude did not respond within timeout")

        return self._turn_output or self._session.get_turn_output()

    def notify_turn_complete(self, output_hint: str = "") -> None:
        """Called by hook server when Stop hook fires."""
        self._session.mark_turn_complete()
        if output_hint:
            self._turn_output = output_hint
        else:
            # Full output accumulated during BUSY state, not just recent tail
            self._turn_output = PTYSession._strip_ansi(self._session._turn_output)
        self._turn_event.set()

    def get_status(self) -> dict:
        return self._session.status_dict()

    def get_recent_output(self, raw: bool = False) -> str:
        return self._session.get_recent_output(raw=raw)

    def get_recent_output_bytes(self) -> bytes:
        return self._session.get_recent_output_bytes()

    def set_output_callback(self, cb: callable) -> None:
        self._session.set_output_callback(cb)

    def write_raw(self, data: bytes) -> None:
        self._session.write_raw(data)
