"""System-tray icon and its menu. The logic lives in TrayActions so it can be tested without a screen."""

from __future__ import annotations

from typing import Any, Callable

from ..pc.store import PcSettingsStore

Notify = Callable[[str, str], None]


class TrayActions:
    def __init__(
        self,
        handle: Any,
        autostart: Any,
        pc_store: PcSettingsStore,
        notify: Notify,
        open_window: Callable[[str], None],
        stop_tray: Callable[[], None],
    ) -> None:
        self.handle, self.autostart, self.pc_store = handle, autostart, pc_store
        self.notify, self._open_window, self._stop_tray = notify, open_window, stop_tray

    # -- state shown as check marks ---------------------------------------------

    @property
    def voice_on(self) -> bool:
        return bool(self.handle.runner.voice_active)

    @property
    def pc_on(self) -> bool:
        return self.pc_store.load().enabled

    @property
    def autostart_on(self) -> bool:
        return self.autostart.is_enabled()

    @property
    def autostart_available(self) -> bool:
        return self.autostart.available

    # -- menu actions -------------------------------------------------------------

    def open(self) -> None:
        self._open_window(self.handle.url)

    def toggle_voice(self) -> None:
        want = not self.voice_on
        self.handle.runner.start(with_voice=want, on_state=self._voice_state if want else None)

    def _voice_state(self, kind: str, reason: str | None) -> None:
        if kind == "ready":
            self.notify("음성 모드", "음성 모드가 켜졌습니다. “자비스, …” 라고 불러 보세요.")
        elif kind == "failed":
            why = reason or "알 수 없는 이유"
            if "No module named" in why:
                why = f"이 설치본에는 음성 구성요소가 들어 있지 않습니다. ({why})"
            self.notify("음성 모드를 켤 수 없습니다", f"{why}\n텍스트 비서는 계속 동작합니다.")

    def toggle_pc_control(self) -> None:
        on = not self.pc_on
        self.pc_store.update(enabled=on)
        if on:
            self.notify("PC 제어 켜짐", "파일 정리·검색·프로그램 열기는 바로 실행하고, 삭제·결제·계정 관련은 먼저 확인합니다.")
        else:
            self.notify("PC 제어 꺼짐", "자비스가 더는 PC를 조작하지 않습니다.")

    def toggle_autostart(self) -> None:
        if not self.autostart_available:
            self.notify("자동 실행", "자동 실행은 Windows에서만 설정할 수 있습니다.")
            return
        try:
            self.autostart.disable() if self.autostart_on else self.autostart.enable()
        except Exception as exc:  # e.g. blocked by a policy or security software
            self.notify("자동 실행을 바꾸지 못했습니다", str(exc))

    def quit(self) -> None:
        self._stop_tray()


def make_icon(size: int = 64):
    """The JARVIS icon (same design as the web app's icon.svg), drawn with Pillow."""
    from PIL import Image, ImageDraw

    scale = 4  # draw big, then shrink: smooth edges at tray sizes
    big = size * scale
    k = big / 512
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = (76, 195, 255, 255)

    def ring(r: float, width: float, alpha: int = 255) -> None:
        c, rr, w = 256 * k, r * k, max(1, round(width * k))
        d.ellipse([c - rr, c - rr, c + rr, c + rr], outline=accent[:3] + (alpha,), width=w)

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=112 * k, fill=(11, 18, 32, 255))
    ring(150, 18, 90)
    ring(104, 14)
    c, r = 256 * k, 46 * k
    d.ellipse([c - r, c - r, c + r, c + r], fill=accent)
    for x1, y1, x2, y2 in ((256, 70, 256, 118), (256, 394, 256, 442), (70, 256, 118, 256), (394, 256, 442, 256)):
        d.line([x1 * k, y1 * k, x2 * k, y2 * k], fill=accent, width=round(14 * k))
    return img.resize((size, size), Image.LANCZOS)


def build_menu(actions: TrayActions):
    import pystray

    item = pystray.MenuItem
    return pystray.Menu(
        item("JARVIS 열기", lambda: actions.open(), default=True),
        item("음성 모드", lambda: actions.toggle_voice(), checked=lambda i: actions.voice_on),
        item("PC 제어 허용", lambda: actions.toggle_pc_control(), checked=lambda i: actions.pc_on),
        item(
            "Windows 시작 시 자동 실행",
            lambda: actions.toggle_autostart(),
            checked=lambda i: actions.autostart_on,
            enabled=lambda i: actions.autostart_available,
        ),
        pystray.Menu.SEPARATOR,
        item("종료", lambda: actions.quit()),
    )


def run_tray(make_actions: Callable[[Notify, Callable[[], None]], TrayActions]) -> None:
    """Show the tray icon and block until "종료". make_actions gets (notify, stop_tray)."""
    import pystray

    holder: dict[str, Any] = {}

    def notify(title: str, message: str) -> None:
        icon = holder.get("icon")
        if icon is not None:
            try:
                icon.notify(message, title)
            except Exception:  # some shells cannot show balloons; the action itself still happened
                pass

    actions = make_actions(notify, lambda: holder["icon"].stop())
    holder["icon"] = pystray.Icon("jarvis", make_icon(64), "JARVIS", menu=build_menu(actions))
    holder["icon"].run()
