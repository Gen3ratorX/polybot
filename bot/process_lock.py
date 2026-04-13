from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import fcntl


@contextmanager
def acquire_database_lock(database_url: str, *, suffix: str = ".lock"):
    lock_path = _lock_path_from_database_url(database_url, suffix=suffix)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_path_from_database_url(database_url: str, *, suffix: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError("Only sqlite URLs are supported for database locks")
    if not parsed.path:
        raise ValueError("sqlite database URL must include a path")
    raw_path = parsed.path
    if raw_path.startswith("/"):
        raw_path = raw_path[1:]
    database_path = Path(raw_path)
    return database_path.with_name(database_path.name + suffix)
