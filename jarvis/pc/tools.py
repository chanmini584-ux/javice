"""The PC-control tools handed to Claude.

Everyday actions (list, search, organize, open an app/page/document) run at once.
Deleting files, unknown programs and payment/account pages are the only things that stop
for the owner's confirmation. The whole feature is off until switched on in settings, and
every action is written to the action log by the agent loop.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable

from ..tools.registry import Risk, ToolError, ToolRegistry
from . import files
from .rules import PROGRAM_SUFFIXES, resolve_in_roots, url_risk
from .store import AuditLog, PcSettingsStore

DEFAULT_APPS = {
    "notepad": "notepad.exe", "메모장": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe", "계산기": "calc.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "탐색기": "explorer.exe", "파일 탐색기": "explorer.exe",
    "paint": "mspaint.exe", "그림판": "mspaint.exe",
    "chrome": "chrome", "크롬": "chrome",
    "edge": "msedge", "엣지": "msedge",
    "firefox": "firefox", "파이어폭스": "firefox",
    "word": "winword", "워드": "winword",
    "excel": "excel", "엑셀": "excel",
    "powerpoint": "powerpnt", "파워포인트": "powerpnt",
    "vscode": "code", "vs code": "code", "visual studio code": "code",
}
APP_NAME = re.compile(r"^[\w .+\-]{1,60}$")  # a plain name: no path, no shell characters


def _default_launcher(command: str) -> None:
    if sys.platform == "win32":
        os.startfile(command)  # type: ignore[attr-defined]  # noqa: S606 - names come from the allow-list
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", command])
    else:
        subprocess.Popen([command], start_new_session=True)


def _default_opener(target: str) -> None:
    if target.lower().startswith(("http://", "https://")):
        webbrowser.open(target)
    elif sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target], start_new_session=True)


class PcControl:
    def __init__(
        self,
        data_dir: Path,
        launcher: Callable[[str], None] | None = None,
        opener: Callable[[str], None] | None = None,
        home: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.settings = PcSettingsStore(self.data_dir / "pc_control.json", home=home)
        self.audit = AuditLog(self.data_dir / "pc_audit.jsonl")
        self.launcher = launcher or _default_launcher
        self.opener = opener or _default_opener

    # -- helpers ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.load().enabled

    def _require_on(self):
        cfg = self.settings.load()
        if not cfg.enabled:
            raise ToolError("PC 제어가 꺼져 있습니다. 앱의 설정 > PC 제어에서 켜 주세요.")
        return cfg

    def _path(self, path: str, cfg) -> Path:
        return resolve_in_roots(path, cfg.roots, deny=[self.data_dir])

    def _apps(self, cfg) -> dict[str, str]:
        return {**DEFAULT_APPS, **cfg.apps}

    def known_app(self, name: str) -> bool:
        return name.strip().lower() in self._apps(self.settings.load())

    def gate(self, fn: Callable[[dict[str, Any]], Risk]) -> Callable[[dict[str, Any]], Risk]:
        """While PC control is off nothing may ask for approval: the call just fails with the reason."""
        return lambda i: fn(i) if self.enabled else "safe"


def register_pc_tools(reg: ToolRegistry, pc: PcControl) -> None:
    safe = pc.gate(lambda i: "safe")
    audited = dict(audited=True)

    @reg.tool(
        "pc_list_folder",
        "List the files and folders inside a folder on the owner's PC (only inside the allowed folders).",
        {"folder": {"type": "string", "description": "Absolute path, or a name like Downloads"}},
        risk_fn=safe, **audited,
    )
    def pc_list_folder(folder: str) -> str:
        cfg = pc._require_on()
        path = pc._path(folder, cfg)
        if not path.is_dir():
            raise ToolError(f"폴더가 아닙니다: {path}")
        rows = []
        for entry in sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:200]:
            rows.append(f"[폴더] {entry.name}" if entry.is_dir() else f"{entry.name}  ({files.human_size(entry.stat().st_size)})")
        return f"{path}\n" + ("\n".join(rows) if rows else "(비어 있음)")

    @reg.tool(
        "pc_search_files",
        "Search file names on the owner's PC. A plain word matches part of the name; wildcards such as *.pdf work. "
        "Searches every allowed folder unless `folder` is given. Newest first.",
        {
            "query": {"type": "string"},
            "folder": {"type": "string", "description": "Optional: search only here"},
            "limit": {"type": "integer", "description": "How many results (1-50, default 20)"},
        },
        required=["query"],
        risk_fn=safe, **audited,
    )
    def pc_search_files(query: str, folder: str = "", limit: int = 20) -> str:
        cfg = pc._require_on()
        roots = [pc._path(folder, cfg)] if folder else [Path(r) for r in cfg.roots if Path(r).is_dir()]
        found = files.search_files(roots, query, limit=max(1, min(int(limit), 50)), deny=[pc.data_dir])
        if not found:
            return "일치하는 파일이 없습니다."
        lines = [f"{f['path']}  ({f['size']}, {f['modified']})" for f in found]
        return f"{len(found)}개 찾음 (최신순):\n" + "\n".join(lines)

    @reg.tool(
        "pc_organize_files",
        "Sort the files directly inside a folder into category subfolders (images, documents, videos, music, "
        "archives, programs, code, other). Only moves files, never overwrites or deletes, skips hidden and "
        "sensitive files, and can be undone with pc_undo_organize. Use dry_run to preview.",
        {"folder": {"type": "string"}, "dry_run": {"type": "boolean", "description": "Preview only"}},
        required=["folder"],
        risk_fn=safe, **audited,
    )
    def pc_organize_files(folder: str, dry_run: bool = False) -> str:
        cfg = pc._require_on()
        res = files.organize(pc._path(folder, cfg), pc.data_dir / "pc_undo", dry_run=bool(dry_run))
        verb = "이동 예정" if res["dry_run"] else "이동 완료"
        cats = ", ".join(f"{k} {v}" for k, v in res["by_category"].items()) or "없음"
        out = f"{verb}: {res['moved']}개 ({cats})"
        if res["truncated"]:
            out += f"\n한 번에 최대 {files.MAX_ORGANIZE}개까지만 처리했습니다. 남은 파일은 다시 실행하세요."
        if res["skipped"]:
            out += "\n건너뜀: " + ", ".join(res["skipped"])
        if res["preview"] and res["dry_run"]:
            out += "\n" + "\n".join(res["preview"])
        return out

    @reg.tool(
        "pc_undo_organize",
        "Undo the most recent pc_organize_files, moving the files back.",
        {}, required=[], risk_fn=safe, **audited,
    )
    def pc_undo_organize() -> str:
        pc._require_on()
        res = files.undo_last_organize(pc.data_dir / "pc_undo")
        return f"되돌림: {res['restored']}개 복원, {res['skipped']}개는 건너뜀 ({res['folder']})"

    @reg.tool(
        "pc_open_app",
        "Open a program on the owner's PC by name (e.g. 메모장, calculator, chrome, excel). Known apps open "
        "immediately; an unknown program name makes the owner confirm first.",
        {"name": {"type": "string"}},
        risk_fn=pc.gate(lambda i: "safe" if pc.known_app(i["name"]) else "confirm"), **audited,
    )
    def pc_open_app(name: str) -> str:
        cfg = pc._require_on()
        key = name.strip().lower()
        if not APP_NAME.match(name.strip()):
            raise ToolError("프로그램 이름만 쓸 수 있습니다 (경로나 명령어는 안 됩니다).")
        command = pc._apps(cfg).get(key)
        if command is None:
            if shutil.which(name.strip()) is None:
                raise ToolError(f"알 수 없는 프로그램입니다: {name}")
            command = name.strip()
        try:
            pc.launcher(command)
        except OSError as exc:
            raise ToolError(f"실행하지 못했습니다: {exc}") from exc
        return f"실행했습니다: {command}"

    @reg.tool(
        "pc_open_url",
        "Open a web page in the owner's browser (http/https only). Pages about payment, sign-in, accounts or "
        "passwords make the owner confirm first. For a web search, open a search-results URL.",
        {"url": {"type": "string"}},
        risk_fn=pc.gate(lambda i: url_risk(i["url"])), **audited,
    )
    def pc_open_url(url: str) -> str:
        pc._require_on()
        url_risk(url)  # raises for anything that is not a web address
        pc.opener(url.strip())
        return f"열었습니다: {url.strip()}"

    @reg.tool(
        "pc_open_path",
        "Open a document, image or folder with its default app. Programs and scripts are refused.",
        {"path": {"type": "string"}},
        risk_fn=safe, **audited,
    )
    def pc_open_path(path: str) -> str:
        cfg = pc._require_on()
        target = pc._path(path, cfg)
        if not target.exists():
            raise ToolError(f"없는 경로입니다: {target}")
        if target.is_file() and target.suffix.lower() in PROGRAM_SUFFIXES:
            raise ToolError("프로그램이나 스크립트는 열지 않습니다.")
        pc.opener(str(target))
        return f"열었습니다: {target}"

    @reg.tool(
        "pc_delete_files",
        "Delete files (not folders): they are moved to a recoverable trash folder. ALWAYS needs the owner's "
        "confirmation, which the system asks for. Up to 50 files per call.",
        {"paths": {"type": "array", "items": {"type": "string"}}},
        risk_fn=pc.gate(lambda i: "confirm"), **audited,
    )
    def pc_delete_files(paths: list[str]) -> str:
        cfg = pc._require_on()
        if not isinstance(paths, list):
            raise ToolError("paths must be a list")
        if len(paths) > files.MAX_TRASH:
            raise ToolError(f"한 번에 최대 {files.MAX_TRASH}개까지만 삭제할 수 있습니다.")
        moved = files.trash([pc._path(p, cfg) for p in paths], pc.data_dir / "trash")
        return f"휴지통 폴더로 옮겼습니다 ({len(moved)}개): {Path(moved[0]['to']).parent}"
