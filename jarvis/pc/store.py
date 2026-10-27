"""Persistent PC-control state: the on/off switch and allowed folders, and the action log.

Both live next to the other JARVIS data and are safe by default: PC control is OFF until
the owner switches it on in the app, and a damaged settings file never turns it on.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

USER_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")


@dataclass
class PcSettings:
    enabled: bool = False
    roots: list[str] = field(default_factory=list)  # folders JARVIS may read and organize
    apps: dict[str, str] = field(default_factory=dict)  # extra apps it may open without asking

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(path: str) -> str:
    return str(Path(path).expanduser().resolve())


class PcSettingsStore:
    def __init__(self, path: Path, home: Path | None = None) -> None:
        self.path = Path(path)
        self.home = Path(home) if home is not None else Path.home()

    def default_roots(self) -> list[str]:
        return [str(self.home / name) for name in USER_FOLDERS]

    def load(self) -> PcSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        roots = raw.get("roots")
        apps = raw.get("apps")
        valid_roots = isinstance(roots, list) and all(isinstance(r, str) for r in roots) and bool(roots)
        valid_apps = isinstance(apps, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in apps.items())
        return PcSettings(
            enabled=raw.get("enabled") is True,  # only a real boolean true switches it on
            roots=list(roots) if valid_roots else self.default_roots(),
            apps=dict(apps) if valid_apps else {},
        )

    def update(
        self,
        enabled: bool | None = None,
        roots: list[str] | None = None,
        apps: dict[str, str] | None = None,
    ) -> PcSettings:
        cfg = self.load()
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise ValueError("enabled는 true 또는 false여야 합니다")
            cfg.enabled = enabled
        if roots is not None:
            if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
                raise ValueError("허용 폴더 목록 형식이 올바르지 않습니다")
            seen: list[str] = []
            for r in roots:
                if r.strip() and _norm(r.strip()) not in seen:
                    seen.append(_norm(r.strip()))
            if not seen:
                raise ValueError("허용할 폴더를 하나 이상 입력하세요")
            cfg.roots = seen
        if apps is not None:
            if not isinstance(apps, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in apps.items()):
                raise ValueError("프로그램 목록 형식이 올바르지 않습니다")
            cfg.apps = {k.strip().lower(): v.strip() for k, v in apps.items() if k.strip() and v.strip()}
        self._save(cfg)
        return cfg

    def _save(self, cfg: PcSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def clip_value(value: Any) -> Any:
    """Shrink a tool input for the log: long strings and lists are cut."""
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "…"
    if isinstance(value, (list, tuple)):
        return [clip_value(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): clip_value(v) for k, v in list(value.items())[:20]}
    return value


class AuditLog:
    """Append-only JSON-lines record of every PC action: what, when, whether it was confirmed."""

    def __init__(self, path: Path, max_bytes: int = 1_000_000) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @property
    def _rotated(self) -> Path:
        return self.path.with_name(self.path.name + ".1")

    def record(self, tool: str, tool_input: dict[str, Any], outcome: str, confirmed: bool = False, detail: str = "") -> None:
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": tool,
            "input": clip_value(tool_input),
            "outcome": outcome,  # ok | error | declined
            "confirmed": confirmed,
        }
        if detail:
            row["detail"] = detail[:300]
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    os.replace(self.path, self._rotated)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # a full or read-only disk must never break the action itself

    def tail(self, n: int = 100) -> list[dict[str, Any]]:
        """The newest ``n`` entries, newest first."""
        lines: list[str] = []
        with self._lock:
            for p in (self._rotated, self.path):
                try:
                    lines += p.read_text(encoding="utf-8").splitlines()
                except OSError:
                    pass
        rows: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
            if len(rows) >= n:
                break
        return rows
