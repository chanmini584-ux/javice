"""Rules that decide what JARVIS may touch and what needs the owner's say-so.

Everything here is pure (no I/O beyond resolving paths) so it is easy to test and audit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

from ..tools.registry import Risk

# Pages about money, accounts or credentials: opening them is not undoable in spirit,
# because what follows (paying, signing in, changing a password) happens right there.
SENSITIVE_URL_WORDS = (
    "pay", "checkout", "billing", "purchase", "card", "bank", "transfer", "order", "wallet",
    "login", "log-in", "logon", "signin", "sign-in", "signup", "sign-up", "account", "password",
    "passwd", "credential", "oauth", "otp", "2fa", "security",
    "결제", "주문", "카드", "은행", "송금", "이체", "로그인", "회원가입", "계정", "비밀번호", "인증", "보안",
)

SENSITIVE_NAME_WORDS = (
    "password", "passwd", "credential", "secret", "private_key", "privatekey", "id_rsa", "id_ed25519",
    "wallet", "keystore", "cookies", "login data", "web data", "token", "api_key", "apikey",
    "비밀번호", "패스워드", "계정", "인증서",
)
SENSITIVE_SUFFIXES = (".pem", ".key", ".kdbx", ".p12", ".pfx", ".ppk", ".keychain", ".jks")

# Opening these would run code, so the "open a file" tool refuses them outright.
PROGRAM_SUFFIXES = frozenset(
    ".exe .bat .cmd .com .msi .msp .scr .ps1 .psm1 .vbs .vbe .js .jse .wsf .wsh .jar .lnk .reg .hta "
    ".cpl .dll .sh .app .command .py .pyw .pyz .url .appref-ms".split()
)

CATEGORIES = {
    "이미지": "jpg jpeg png gif bmp webp heic heif svg tif tiff raw",
    "문서": "pdf doc docx ppt pptx xls xlsx hwp hwpx txt md csv rtf odt ods odp",
    "동영상": "mp4 mkv avi mov wmv webm flv m4v",
    "음악": "mp3 wav flac aac m4a ogg wma",
    "압축파일": "zip rar 7z tar gz tgz bz2 xz",
    "프로그램": "exe msi dmg apk iso pkg deb",
    "코드": "py js ts tsx jsx java c cpp h hpp cs go rs html css json ipynb sql sh bat",
}
_BY_EXT = {f".{ext}": cat for cat, exts in CATEGORIES.items() for ext in exts.split()}
OTHER = "기타"


def category_of(filename: str) -> str:
    return _BY_EXT.get(Path(filename).suffix.lower(), OTHER)


def is_sensitive_name(name: str) -> bool:
    low = name.lower()
    if low == ".env" or low.startswith(".env."):
        return True
    return any(w in low for w in SENSITIVE_NAME_WORDS) or low.endswith(SENSITIVE_SUFFIXES)


def url_risk(url: str) -> Risk:
    """'confirm' for payment/account/credential pages. Raises ValueError for anything but a web URL."""
    if not isinstance(url, str):
        raise ValueError("url must be text")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) web addresses can be opened")
    haystack = unquote(f"{parts.hostname}{parts.path}?{parts.query}").lower()
    return "confirm" if any(w in haystack for w in SENSITIVE_URL_WORDS) else "safe"


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def resolve_in_roots(path: str, roots: Iterable[str], deny: Iterable[str | Path] = ()) -> Path:
    """Turn user/model input into a real path that is inside an allowed folder.

    Symlinks and ``..`` are resolved first, so they cannot be used to step outside.
    A relative path such as "Downloads/a.txt" is looked up under the allowed folders.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("a path is required")
    root_paths = [Path(r).resolve() for r in roots]
    p = Path(path.strip()).expanduser()
    if p.is_absolute():
        candidate = p
    else:
        options: list[Path] = []
        for r in root_paths:
            if p.parts and p.parts[0].lower() == r.name.lower():
                options.append(r.parent / p)
            options.append(r / p)
        found = [c for c in options if c.exists()]
        if not found:
            raise FileNotFoundError(f"허용된 폴더에서 찾을 수 없습니다: {path}")
        candidate = found[0]
    resolved = candidate.resolve()
    for d in deny:
        if _inside(resolved, Path(d).resolve()):
            raise PermissionError("JARVIS 자체 데이터 폴더는 접근할 수 없습니다.")
    if not any(_inside(resolved, r) for r in root_paths):
        raise PermissionError(f"허용된 폴더 밖입니다: {path} (설정 > PC 제어에서 허용 폴더를 바꿀 수 있습니다)")
    return resolved
