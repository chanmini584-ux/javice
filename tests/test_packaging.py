"""Packaging files stay consistent with the code (the real build only runs on Windows)."""

import ast
import re
import struct
from pathlib import Path

import pytest

import jarvis
from jarvis.desktop.autostart import VALUE_NAME

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"


def pyproject():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+; the project also supports 3.10
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_is_the_same_everywhere():
    assert pyproject()["project"]["version"] == jarvis.__version__
    iss = (PKG / "installer.iss").read_text(encoding="utf-8-sig")
    assert re.search(rf'#define AppVersion "{re.escape(jarvis.__version__)}"', iss)


def test_desktop_extras_and_launchers_are_declared():
    p = pyproject()["project"]
    assert any(d.lower().startswith("pystray") for d in p["optional-dependencies"]["desktop"])
    assert any(d.lower().startswith("pillow") for d in p["optional-dependencies"]["desktop"])
    assert p["gui-scripts"]["jarvis-desktop"] == "jarvis.desktop.app:entry"


def test_gui_script_target_exists():
    from jarvis.desktop import app

    assert callable(app.entry)


def test_ico_has_the_sizes_windows_asks_for(tmp_path):
    pytest.importorskip("PIL")
    from jarvis.desktop.tray import save_ico

    out = save_ico(tmp_path / "jarvis.ico")
    data = out.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1) and count >= 5
    sizes = {data[6 + 16 * i] or 256 for i in range(count)}  # width byte: 0 means 256
    assert {16, 32, 48, 256} <= sizes


def test_installer_matches_what_the_app_expects():
    iss = (PKG / "installer.iss").read_text(encoding="utf-8-sig")
    assert "PrivilegesRequired=lowest" in iss  # per-user install: no administrator needed
    assert f'ValueName: "{VALUE_NAME}"' in iss and "--background" in iss  # same Run entry as the tray toggle
    assert r"Software\Microsoft\Windows\CurrentVersion\Run" in iss
    assert "autostart" in iss and "desktopicon" in iss
    assert "DeleteValue" in iss  # uninstall removes an entry the tray toggle created too
    assert (PKG / "installer.iss").read_bytes().startswith(b"\xef\xbb\xbf")  # Inno needs a BOM for Korean text


def test_pyinstaller_spec_is_valid_python_and_points_at_real_files():
    src = (PKG / "jarvis.spec").read_text(encoding="utf-8")
    ast.parse(src)
    assert "jarvis_desktop.py" in src and (PKG / "jarvis_desktop.py").exists()
    assert "console=False" in src  # a double-click app has no black console window
    assert "web/static" in src  # the web app files must be bundled
    for heavy in ("torch", "speechbrain", "faster_whisper", "ultralytics", "cv2"):
        assert heavy in src  # excluded unless the voice build is requested
    assert "JARVIS_BUILD_VOICE" in src


def test_entry_script_calls_the_exiting_entry_point():
    src = (PKG / "jarvis_desktop.py").read_text(encoding="utf-8")
    assert "entry()" in src


def test_build_script_runs_icon_pyinstaller_and_inno_in_order():
    lines = (PKG / "build_windows.ps1").read_text(encoding="utf-8").splitlines()
    ps1 = "\n".join(l for l in lines if not l.lstrip().startswith("#"))  # the usage comment mentions pyinstaller too
    order = [ps1.index(s) for s in ("make_icon.py", "pyinstaller", "ISCC")]
    assert order == sorted(order)
    assert "$ErrorActionPreference" in ps1


def test_ci_builds_on_windows_and_tests_first():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["build"]
    assert job["runs-on"] == "windows-latest"
    steps = [s.get("run", "") + s.get("uses", "") for s in job["steps"]]
    joined = "\n".join(steps)
    assert joined.index("pytest") < joined.index("build_windows.ps1") < joined.index("upload-artifact")
    assert "workflow_dispatch" in (wf.get("on") or wf.get(True))


def test_build_outputs_are_git_ignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("dist/", "build/", "dist-installer/", "*.ico"):
        assert pattern in ignore
