"""Instance ownership and bounded, privacy-conscious operational logging."""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import time
from pathlib import Path

from .config import CleanerError


class InstanceLock:
    def __init__(self, path: Path):
        self.file = path.open("a+b")
        try:
            # Windows mandatory byte locks also prohibit reading an already locked byte.
            # Inspect metadata instead; all owners lock offset zero even on first-create races.
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise CleanerError("已有一个清理插件实例在运行，请等待旧实例退出。") from exc

    def close(self):
        self.file.close()


class CheckedHandler(logging.handlers.RotatingFileHandler):
    healthy = True

    def handleError(self, record):
        self.healthy = False


class Journal:
    def __init__(self, root: Path):
        self.root = root
        self.directory = root / "logs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handler = CheckedHandler(
            self.directory / "cleaner.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=7,
            encoding="utf-8",
            delay=False,
        )
        formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
        formatter.converter = time.gmtime
        self.handler.setFormatter(formatter)
        self.logger = logging.Logger(f"qq-cleaner-{id(self)}", level=logging.INFO)
        self.logger.addHandler(self.handler)
        self.last = {}

    def record(self, kind: str, detail: str = ""):
        # Callers supply fixed error categories and random plan IDs, never raw API exceptions.
        key = (kind, detail)
        now = time.time()
        previous, skipped = self.last.get(key, (0, 0))
        if now - previous < 300:
            self.last[key] = (previous, skipped + 1)
            return
        if len(self.last) >= 256:
            self.last.clear()
        self.last[key] = (now, 0)
        self.logger.info("%s %s%s", kind[:100], detail[:1000], f"（重复 {skipped} 次）" if skipped else "")

    def check(self):
        if not self.handler.healthy:
            raise CleanerError("插件日志无法写入或轮转，已停止清理，请检查磁盘和文件占用。")
        if shutil.disk_usage(self.root).free < 256 * 1024 * 1024:
            raise CleanerError("插件数据盘剩余空间不足 256 MiB，已停止清理。")
        total = sum(p.stat().st_size for p in self.root.glob("state.sqlite3*"))
        if total > 512 * 1024 * 1024:
            raise CleanerError("插件数据库超过容量上限，已停止清理，请检查并归档历史记录。")

    def maintain(self):
        for path in self.directory.glob("cleaner.log.*"):
            if path.suffix[1:].isdigit() and path.stat().st_mtime < time.time() - 14 * 86400:
                path.unlink()
        self.check()

    def close(self):
        self.handler.flush()
        self.handler.close()
        self.logger.removeHandler(self.handler)
