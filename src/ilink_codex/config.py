from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    state_dir: Path
    work_dir: Path
    codex_bin: str = "codex"
    max_concurrency: int = 1
    task_timeout: int = 600
    max_pending_per_user: int = 10
    max_prompt_chars: int = 16000
    reply_chunk_chars: int = 1000
    max_reply_chars: int = 12000
    max_send_attempts: int = 5

    @classmethod
    def load(cls, path: Path) -> Config:
        data = tomllib.loads(path.read_text())
        for key in ("state_dir", "work_dir"):
            data[key] = (path.resolve().parent / data.get(key, key.removesuffix("_dir"))).resolve()
        cfg = cls(**data)
        if cfg.state_dir.is_relative_to(cfg.work_dir) or cfg.work_dir.is_relative_to(cfg.state_dir):
            raise ValueError("state_dir and work_dir must be separate directories")
        for key in ("max_concurrency", "task_timeout", "max_pending_per_user", "max_prompt_chars",
                    "reply_chunk_chars", "max_reply_chars", "max_send_attempts"):
            if type(getattr(cfg, key)) is not int or getattr(cfg, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if cfg.max_concurrency > 5 or cfg.reply_chunk_chars > 1000:
            raise ValueError("max_concurrency <= 5; reply_chunk_chars <= 1000")
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return cfg

    def workspace(self, account: str, user: str) -> Path:
        key = hashlib.sha256(json.dumps([account, user]).encode()).hexdigest()[:32]
        path = self.work_dir / key
        if path.is_symlink():
            raise ValueError("workspace must not be a symlink")
        path.mkdir(mode=0o700, exist_ok=True)
        return path


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Allowlist:
    """Reload on every check; missing/corrupt/empty config fails closed."""

    def __init__(self, path: Path):
        self.path = path

    def allows(self, account: str, user: str) -> bool:
        try:
            data = json.loads(self.path.read_text())
            users = data[account]
            return bool(user) and isinstance(users, list) and user in users
        except (OSError, ValueError, KeyError, TypeError):
            return False
