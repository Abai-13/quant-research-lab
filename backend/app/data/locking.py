"""写入串行锁。

同一 worktree 的数据写入必须串行：并发写入由本模块的有界等待与明确错误处理，
不终止其他进程，也不让两个导入批次互相覆盖。锁文件带 PID 与时间戳，便于诊断。
"""

from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import DataError


@dataclass
class LockInfo:
    path: Path
    holder: str
    acquired_at: float


class DataWriteLock:
    """基于 ``O_CREAT|O_EXCL`` 的文件锁。"""

    def __init__(
        self, path: Path, *, wait_seconds: float = 30.0, poll_seconds: float = 0.25
    ) -> None:
        self.path = Path(path)
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._fd: int | None = None

    def _read_holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unknown"

    def acquire(self) -> LockInfo:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._wait_seconds
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise DataError("write_lock_failed", f"无法创建写入锁: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise DataError(
                        "write_lock_timeout",
                        f"已有数据导入正在写入（{self._read_holder()}），"
                        f"等待 {self._wait_seconds} 秒后仍未获得锁",
                    )
                time.sleep(self._poll_seconds)
                continue
            payload = f"pid={os.getpid()} acquired_at={time.time():.3f}"
            os.write(fd, payload.encode("utf-8"))
            self._fd = fd
            return LockInfo(path=self.path, holder=payload, acquired_at=time.time())

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> LockInfo:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = ["DataWriteLock", "LockInfo"]
