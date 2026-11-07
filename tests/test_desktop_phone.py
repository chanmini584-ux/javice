import json

import pytest

from jarvis.desktop import phone
from jarvis.desktop.phone import PhoneAccess, connect, find_tailscale, page, show

RUNNING = {"BackendState": "Running", "Self": {"DNSName": "my-pc.tail1234.ts.net."}}


class FakeTailscale:
    def __init__(self, status=RUNNING, serve=(0, "Available within your tailnet")):
        self.status, self.serve, self.calls = status, serve, []

    def __call__(self, args, timeout):
        self.calls.append(list(args[1:]))
        if args[1] == "status":
            return (0, json.dumps(self.status)) if self.status is not None else (1, "failed to connect to local tailscaled")
        return self.serve


def test_ready_serves_the_local_port_over_https_and_gives_the_phone_address():
    ts = FakeTailscale()
    access = connect(8765, "tok", run=ts, find=lambda: "tailscale")
    assert access == PhoneAccess("ready", url="https://my-pc.tail1234.ts.net/#token=tok")
    assert ts.calls == [["status", "--json"], ["serve", "--bg", "8765"]]


def test_no_tailscale_asks_to_install_it_and_runs_nothing():
    ts = FakeTailscale()
    assert connect(8765, "tok", run=ts, find=lambda: None).state == "missing"
    assert ts.calls == []


@pytest.mark.parametrize("status", [None, {"BackendState": "NeedsLogin"}, {"BackendState": "Stopped"}])
def test_logged_out_or_stopped_tailscale_is_not_served(status):
    ts = FakeTailscale(status=status)
    assert connect(8765, "tok", run=ts, find=lambda: "tailscale").state == "logged_out"
    assert ["serve", "--bg", "8765"] not in ts.calls


def test_https_not_enabled_on_the_tailnet_points_to_the_approval_page():
    out = "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n         https://login.tailscale.com/f/serve?node=abc123\n"
    access = connect(8765, "tok", run=FakeTailscale(serve=(-1, out)), find=lambda: "tailscale")
    assert access.state == "needs_approval"
    assert access.link == "https://login.tailscale.com/f/serve?node=abc123"


def test_other_serve_errors_are_reported_with_tailscales_words():
    access = connect(8765, "tok", run=FakeTailscale(serve=(1, "Access denied: serve config denied")), find=lambda: "tailscale")
    assert access.state == "failed" and "Access denied" in access.detail


def test_missing_magicdns_name_is_a_clear_failure():
    access = connect(8765, "tok", run=FakeTailscale(status={"BackendState": "Running", "Self": {}}), find=lambda: "tailscale")
    assert access.state == "failed" and "MagicDNS" in access.detail


def test_find_tailscale_prefers_path_then_the_default_install_folder():
    assert find_tailscale(which=lambda n: "/usr/bin/tailscale", exists=lambda p: False, env={}) == "/usr/bin/tailscale"
    found = find_tailscale(which=lambda n: None, exists=lambda p: p.endswith("tailscale.exe"), env={"ProgramFiles": "C:\\PF"})
    assert found == "C:\\PF\\Tailscale\\tailscale.exe"
    assert find_tailscale(which=lambda n: None, exists=lambda p: False, env={}) is None


def test_ready_page_has_the_qr_for_the_address_and_install_steps():
    seen = []
    html = page(PhoneAccess("ready", url="https://my-pc.ts.net/#token=tok"), qr=lambda d: seen.append(d) or "<svg>QR</svg>")
    assert seen == ["https://my-pc.ts.net/#token=tok"]
    assert "<svg>QR</svg>" in html and "홈 화면에 추가" in html and "앱 설치" in html
    assert "tailscale serve reset" in html


@pytest.mark.parametrize(
    "access, expect",
    [
        (PhoneAccess("missing"), "tailscale.com/download"),
        (PhoneAccess("logged_out"), "Log in"),
        (PhoneAccess("needs_approval", link="https://login.tailscale.com/f/serve?node=x"), "login.tailscale.com/f/serve"),
        (PhoneAccess("failed", detail="<b>boom</b>"), "&lt;b&gt;boom&lt;/b&gt;"),
    ],
)
def test_every_other_state_says_the_one_thing_left_to_do(access, expect):
    html = page(access, qr=lambda d: pytest.fail("no QR before the phone can connect"))
    assert expect in html


def test_real_qr_code_is_an_inline_svg():
    pytest.importorskip("qrcode")
    svg = phone.qr_svg("https://my-pc.ts.net/#token=tok")
    assert svg.startswith("<svg") and "<?xml" not in svg


def test_show_writes_a_private_page_and_opens_it(tmp_path):
    opened = []
    access = show(
        tmp_path, 8765, "tok", opened.append,
        connect_fn=lambda port, token: PhoneAccess("missing"),
    )
    assert access.state == "missing"
    path = tmp_path / "phone.html"
    assert path.exists() and opened == [path.resolve().as_uri()]
