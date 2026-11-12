"""Using JARVIS from a phone: publish the app on the owner's tailnet over HTTPS and show a QR code.

Phones only install the web app (and use the mic) over HTTPS. Tailscale gives every PC an HTTPS
name that only the owner's own devices can reach, so the server can keep listening on 127.0.0.1:
`tailscale serve` forwards to it. Nothing is opened to the internet or the local network.
"""

from __future__ import annotations

import html
import json
import ntpath
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

Run = Callable[[Sequence[str], float], "tuple[int, str]"]

INSTALL_URL = "https://tailscale.com/download"
_LINK = re.compile(r"https://login\.tailscale\.com/\S+")


def _run(args: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run a command and return (exit code, stdout+stderr). A timeout or missing exe is code -1."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash from a windowed app
    try:
        p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return -1, out
    except OSError as exc:
        return -1, str(exc)


def find_tailscale(
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    env: Mapping[str, str] = os.environ,
) -> str | None:
    found = which("tailscale")
    if found:
        return found
    for base in (env.get("ProgramFiles", "C:\\Program Files"), env.get("ProgramFiles(x86)", "C:\\Program Files (x86)")):
        path = ntpath.join(base, "Tailscale", "tailscale.exe")
        if exists(path):
            return path
    return None


@dataclass
class PhoneAccess:
    state: str  # "ready" | "missing" | "logged_out" | "needs_approval" | "failed"
    url: str | None = None  # https://<pc>.<tailnet>.ts.net/#token=... when ready
    link: str | None = None  # Tailscale page to approve HTTPS, when needs_approval
    detail: str = ""


def connect(port: int, token: str, run: Run = _run, find: Callable[[], str | None] = find_tailscale) -> PhoneAccess:
    """Make JARVIS reachable from the owner's phone and return the address to open there."""
    exe = find()
    if not exe:
        return PhoneAccess("missing")
    code, out = run([exe, "status", "--json"], 15)
    try:
        status = json.loads(out) if code == 0 else {}
    except ValueError:
        status = {}
    if status.get("BackendState") != "Running":
        return PhoneAccess("logged_out", detail=out.strip()[:300])
    name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        return PhoneAccess("failed", detail="이 PC의 Tailscale 주소(MagicDNS)를 찾지 못했습니다.")
    # --bg keeps forwarding after this command returns, and across restarts, until `tailscale serve reset`.
    code, out = run([exe, "serve", "--bg", str(port)], 30)
    if code != 0:
        link = _LINK.search(out)
        if link:  # HTTPS certificates are off for this tailnet: the owner allows them once on this page
            return PhoneAccess("needs_approval", link=link.group(0).rstrip(".,)"), detail=out.strip()[:300])
        return PhoneAccess("failed", detail=out.strip()[:300] or "tailscale serve가 응답하지 않았습니다.")
    return PhoneAccess("ready", url=f"https://{name}/#token={token}")


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    svg = img.to_string(encoding="unicode")
    return svg[svg.index("<svg"):]  # drop the XML declaration so it can sit inside HTML


_STYLE = """
body{font-family:'Segoe UI','Malgun Gothic',sans-serif;background:#0b1220;color:#e6edf6;margin:0;padding:32px 20px;line-height:1.6}
main{max-width:560px;margin:0 auto}h1{font-size:22px;margin:0 0 6px}p,li{color:#c3cfdd}
.qr{background:#fff;border-radius:16px;padding:14px;width:260px;margin:22px auto}.qr svg{width:100%;height:auto;display:block}
ol{padding-left:22px}code{background:#16233a;padding:1px 6px;border-radius:6px;color:#4cc3ff;word-break:break-all}
a{color:#4cc3ff}.note{font-size:13px;color:#8fa0b5;border-top:1px solid #1d2c45;margin-top:28px;padding-top:14px}
"""


def page(access: PhoneAccess, qr: Callable[[str], str] = qr_svg) -> str:
    """The help page the tray opens: a QR code when ready, otherwise the one thing left to do."""
    e = html.escape
    if access.state == "ready" and access.url:
        body = f"""<h1>폰에서 JARVIS 쓰기</h1>
<p>폰 카메라로 아래 QR을 찍어 여세요. 폰에도 Tailscale 앱이 같은 계정으로 켜져 있어야 합니다.</p>
<div class="qr">{qr(access.url)}</div>
<ol><li>열리면 <b>아이폰</b>: Safari 공유 버튼 → <b>홈 화면에 추가</b></li>
<li><b>안드로이드</b>: Chrome 메뉴(⋮) → <b>앱 설치</b> 또는 <b>홈 화면에 추가</b></li>
<li>이제 홈 화면의 JARVIS 아이콘으로 바로 열립니다. PC에서 JARVIS가 켜져 있어야 답합니다.</li></ol>
<p class="note">이 QR에는 접속 토큰이 들어 있습니다. 다른 사람에게 보여 주지 마세요.
주소는 내 Tailscale 기기끼리만 열리고, 인터넷에는 공개되지 않습니다.
폰 연결을 끊으려면 PC cmd에서 <code>tailscale serve reset</code>을 실행하세요.</p>"""
    elif access.state == "missing":
        body = f"""<h1>폰 연결 준비: Tailscale 설치</h1>
<p>폰에서 JARVIS를 앱처럼 쓰려면 PC와 폰을 Tailscale(무료)로 이어야 합니다. 한 번만 하면 됩니다.</p>
<ol><li><a href="{INSTALL_URL}">tailscale.com/download</a>에서 <b>이 PC</b>에 설치하고 로그인하세요.</li>
<li>폰에도 앱스토어/플레이스토어에서 <b>Tailscale</b>을 깔고 <b>같은 계정</b>으로 로그인하세요.</li>
<li>트레이의 JARVIS 아이콘 → <b>폰에서 쓰기</b>를 다시 누르세요.</li></ol>"""
    elif access.state == "logged_out":
        body = """<h1>폰 연결 준비: Tailscale 로그인</h1>
<p>Tailscale은 설치돼 있지만 이 PC가 로그인되어 있지 않거나 꺼져 있습니다.</p>
<ol><li>작업 표시줄 오른쪽의 Tailscale 아이콘 → <b>Log in</b>(또는 Connect)을 누르세요.</li>
<li>폰의 Tailscale 앱도 같은 계정으로 켜 두세요.</li>
<li>트레이의 JARVIS 아이콘 → <b>폰에서 쓰기</b>를 다시 누르세요.</li></ol>"""
    elif access.state == "needs_approval" and access.link:
        body = f"""<h1>폰 연결 준비: HTTPS 허용 (한 번만)</h1>
<p>폰에 앱으로 설치하려면 HTTPS 주소가 필요한데, 이 Tailscale 계정에서 아직 꺼져 있습니다.</p>
<ol><li><a href="{e(access.link)}">이 링크</a>를 열고 <b>Enable</b>을 누르세요.</li>
<li>트레이의 JARVIS 아이콘 → <b>폰에서 쓰기</b>를 다시 누르세요.</li></ol>"""
    else:
        body = f"""<h1>폰 연결을 마치지 못했습니다</h1>
<p>Tailscale이 이렇게 답했습니다.</p><p><code>{e(access.detail or "알 수 없는 오류")}</code></p>
<p>PC와 폰의 Tailscale이 같은 계정으로 켜져 있는지 확인한 뒤 다시 시도해 주세요.</p>"""
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JARVIS · 폰에서 쓰기</title>
<style>{_STYLE}</style></head><body><main>{body}</main></body></html>"""


def show(
    data_dir: Path,
    port: int,
    token: str,
    open_page: Callable[[str], object],
    connect_fn: Callable[[int, str], PhoneAccess] = connect,
) -> PhoneAccess:
    """Connect, write the help page next to the other private files, and open it on this PC."""
    access = connect_fn(port, token)
    path = Path(data_dir) / "phone.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page(access), encoding="utf-8")
    try:
        path.chmod(0o600)  # the page carries the token
    except OSError:
        pass
    open_page(path.resolve().as_uri())
    return access
