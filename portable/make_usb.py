"""Build a portable JARVIS folder on a USB stick (or any directory).

    python portable/make_usb.py E:\\           # Windows
    python portable/make_usb.py /Volumes/USB   # macOS
    python portable/make_usb.py /media/me/USB --download-uv --extras web,voice

Layout written to <target>/JARVIS:
    app/                 source code (this repository's package)
    data/                JARVIS_HOME: memory, knowledge, voiceprint, tokens (kept between PCs)
    bin/<os>/uv[.exe]    optional: uv, which installs Python and packages on any PC
    start-windows.bat, start-mac.command, start-linux.sh

Re-running updates app/ and the launchers and never touches data/.
"""

from __future__ import annotations

import argparse
import io
import shutil
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UV_ASSETS = {
    "windows": ("uv-x86_64-pc-windows-msvc.zip", "uv.exe"),
    "macos": ("uv-aarch64-apple-darwin.tar.gz", "uv"),
    "linux": ("uv-x86_64-unknown-linux-gnu.tar.gz", "uv"),
}
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/{asset}"
LAUNCHERS = ("start-windows.bat", "start-mac.command", "start-linux.sh")


def copy_app(dest: Path) -> None:
    app = dest / "app"
    if app.exists():
        shutil.rmtree(app)
    shutil.copytree(REPO / "jarvis", app / "jarvis", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(REPO / name, app / name)


def copy_launchers(dest: Path) -> None:
    for name in LAUNCHERS:
        target = dest / name
        shutil.copy2(REPO / "portable" / name, target)
        try:
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass  # FAT/exFAT: no permission bits; macOS/Linux users may need `bash start-*.sh`


def download_uv(dest: Path) -> None:
    for os_name, (asset, exe) in UV_ASSETS.items():
        url = UV_URL.format(asset=asset)
        print(f"downloading {url}")
        data = urllib.request.urlopen(url, timeout=60).read()
        out = dest / "bin" / os_name
        out.mkdir(parents=True, exist_ok=True)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                member = next(n for n in z.namelist() if n.endswith(exe))
                (out / exe).write_bytes(z.read(member))
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
                member = next(m for m in t.getmembers() if m.name.endswith("/" + exe) or m.name == exe)
                (out / exe).write_bytes(t.extractfile(member).read())
        try:
            (out / exe).chmod(0o755)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="USB drive root or any folder")
    parser.add_argument("--download-uv", action="store_true", help="bundle uv for Windows, macOS (Apple silicon), Linux")
    parser.add_argument("--extras", default="web", help="comma-separated extras: web,voice,vision")
    args = parser.parse_args()

    dest = args.target / "JARVIS"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "data").mkdir(exist_ok=True)
    copy_app(dest)
    copy_launchers(dest)
    if args.extras != "web":
        (dest / "data" / "extras.txt").write_text(args.extras, encoding="utf-8")
    if args.download_uv:
        download_uv(dest)
    print(f"\nready: {dest}")
    print("run start-windows.bat / start-mac.command / start-linux.sh on any PC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
