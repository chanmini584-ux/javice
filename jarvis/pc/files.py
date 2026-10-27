"""File operations for PC control. Nothing here deletes data: files are only moved, and every
move can be undone (organize keeps a manifest, delete goes to a trash folder)."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from ..tools.registry import ToolError
from .rules import category_of, is_sensitive_name

MAX_ORGANIZE = 500
MAX_TRASH = 50
KEEP_MANIFESTS = 20
SKIP_DIRS = {"node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"}
UNFINISHED = {".crdownload", ".part", ".partial", ".tmp", ".download"}
SYSTEM_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _unique(dst: Path, taken: set[Path]) -> Path:
    if not dst.exists() and dst not in taken:
        return dst
    n = 1
    while True:
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        if not cand.exists() and cand not in taken:
            return cand
        n += 1


def search_files(roots: Iterable[Path], query: str, limit: int = 20, deny: Iterable[Path] = (), max_visited: int = 50_000) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        raise ValueError("a search word is required")
    is_glob = any(c in q for c in "*?[")
    deny = [Path(d).resolve() for d in deny]
    hits: list[tuple[float, Path, int]] = []
    visited = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIRS and not any(_inside((here / d).resolve(), x) for x in deny)
            ]
            for fn in filenames:
                visited += 1
                if visited > max_visited:
                    break
                if fn.startswith(".") or is_sensitive_name(fn):
                    continue
                low = fn.lower()
                if fnmatch.fnmatch(low, q) if is_glob else q in low:
                    try:
                        st = (here / fn).stat()
                    except OSError:
                        continue
                    hits.append((st.st_mtime, here / fn, st.st_size))
            if visited > max_visited:
                break
    hits.sort(key=lambda h: h[0], reverse=True)
    return [
        {"path": str(p), "size": human_size(size), "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(m))}
        for m, p, size in hits[:limit]
    ]


def organize(folder: Path, undo_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """Sort the files directly inside ``folder`` into category subfolders."""
    if not folder.is_dir():
        raise ToolError(f"폴더가 아닙니다: {folder}")
    plan: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    taken: set[Path] = set()
    truncated = False
    for entry in sorted(folder.iterdir(), key=lambda e: e.name.lower()):
        name = entry.name
        if entry.is_symlink() or not entry.is_file():
            continue
        if name.startswith(".") or name.lower() in SYSTEM_FILES or entry.suffix.lower() in UNFINISHED:
            skipped.append(name)
            continue
        if is_sensitive_name(name):
            skipped.append(f"{name} (민감한 파일)")
            continue
        if len(plan) >= MAX_ORGANIZE:
            truncated = True
            break
        dst = _unique(folder / category_of(name) / name, taken)
        taken.add(dst)
        plan.append((entry, dst))
    by_category: dict[str, int] = {}
    for _, dst in plan:
        by_category[dst.parent.name] = by_category.get(dst.parent.name, 0) + 1
    result: dict[str, Any] = {
        "moved": len(plan),
        "by_category": by_category,
        "skipped": skipped[:20],
        "truncated": truncated,
        "dry_run": dry_run,
        "preview": [f"{s.name} -> {d.parent.name}/" for s, d in plan[:30]],
    }
    if dry_run or not plan:
        return result
    done: list[dict[str, str]] = []
    try:
        for src, dst in plan:
            dst.parent.mkdir(exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append({"src": str(src), "dst": str(dst)})
    finally:
        if done:
            _write_manifest(undo_dir, str(folder), done)
        result["moved"] = len(done)
    return result


def _write_manifest(undo_dir: Path, folder: str, moves: list[dict[str, str]]) -> None:
    undo_dir.mkdir(parents=True, exist_ok=True)
    (undo_dir / f"organize-{time.time_ns()}.json").write_text(
        json.dumps({"folder": folder, "moves": moves}, ensure_ascii=False), encoding="utf-8"
    )
    for old in sorted(undo_dir.glob("organize-*.json"))[:-KEEP_MANIFESTS]:
        old.unlink(missing_ok=True)


def undo_last_organize(undo_dir: Path) -> dict[str, Any]:
    manifests = sorted(undo_dir.glob("organize-*.json")) if undo_dir.is_dir() else []
    if not manifests:
        raise ToolError("되돌릴 정리 기록이 없습니다.")
    last = manifests[-1]
    data = json.loads(last.read_text(encoding="utf-8"))
    restored = skipped = 0
    for move in reversed(data["moves"]):
        src, dst = Path(move["src"]), Path(move["dst"])
        if dst.is_file() and not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            restored += 1
            try:
                dst.parent.rmdir()  # only succeeds when the category folder is now empty
            except OSError:
                pass
        else:
            skipped += 1
    last.unlink(missing_ok=True)
    return {"restored": restored, "skipped": skipped, "folder": data["folder"]}


def trash(paths: list[Path], trash_dir: Path) -> list[dict[str, str]]:
    """Move files to a dated trash folder. All paths are checked before anything moves."""
    if not paths:
        raise ToolError("삭제할 파일이 없습니다.")
    if len(paths) > MAX_TRASH:
        raise ToolError(f"한 번에 최대 {MAX_TRASH}개까지만 삭제할 수 있습니다.")
    for p in paths:
        if not p.is_file():
            raise ToolError(f"파일이 아니거나 없습니다 (폴더는 삭제하지 않습니다): {p}")
    dest = trash_dir / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    taken: set[Path] = set()
    for p in paths:
        target = _unique(dest / p.name, taken)
        taken.add(target)
        shutil.move(str(p), str(target))
        moved.append({"from": str(p), "to": str(target)})
    return moved
