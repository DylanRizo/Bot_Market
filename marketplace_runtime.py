from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


SCRATCH_DIR = Path(__file__).resolve().parent
LOCKS_DIR = SCRATCH_DIR / "marketplace_runtime_locks"


def safe_key(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value) or "default"


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class AccountLock:
    """Small cross-process lock that prevents two publishers using one profile."""

    def __init__(self, account: str, timeout_seconds: float = 0) -> None:
        self.account = safe_key(account)
        self.timeout_seconds = max(0, timeout_seconds)
        self.path = LOCKS_DIR / f"{self.account}.lock"
        self.acquired = False

    def _remove_stale(self) -> None:
        try:
            payload: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid") or 0)
        except Exception:
            pid = 0
        if not process_exists(pid):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def acquire(self) -> None:
        LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = json.dumps({"pid": os.getpid(), "account": self.account, "created_at": time.time()})
                os.write(descriptor, payload.encode("utf-8"))
                os.close(descriptor)
                self.acquired = True
                return
            except FileExistsError:
                self._remove_stale()
                if not self.path.exists():
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"La cuenta {self.account} ya esta siendo usada por otra publicacion.")
                time.sleep(0.5)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if int(payload.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def __enter__(self) -> AccountLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

