"""PC control tools: file organizing, search, trash, launching, and the rules that gate them."""

import os
import time

import pytest

from jarvis.pc.rules import (
    category_of,
    is_sensitive_name,
    resolve_in_roots,
    url_risk,
)
from jarvis.pc.tools import PcControl, register_pc_tools
from jarvis.tools.registry import ToolRegistry


# --- rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,risk",
    [
        ("https://www.wikipedia.org/wiki/Python", "safe"),
        ("https://news.example.com/article?id=3", "safe"),
        ("https://shop.example.com/checkout", "confirm"),
        ("https://www.paypal.com/", "confirm"),
        ("https://accounts.google.com/signin", "confirm"),
        ("https://bank.example.kr/transfer", "confirm"),
        ("https://example.com/?next=/billing", "confirm"),
        ("https://example.com/settings/password", "confirm"),
        ("https://example.com/결제", "confirm"),
        ("https://example.com/로그인", "confirm"),
    ],
)
def test_url_risk(url, risk):
    assert url_risk(url) == risk


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "not a url", "", "http://"])
def test_url_risk_rejects_non_web_urls(url):
    with pytest.raises(ValueError):
        url_risk(url)


@pytest.mark.parametrize(
    "name,sensitive",
    [
        ("passwords.txt", True),
        ("my_password_list.xlsx", True),
        ("id_rsa", True),
        (".env", True),
        ("server.pem", True),
        ("wallet.dat", True),
        ("vault.kdbx", True),
        ("Login Data", True),
        ("credentials.json", True),
        ("비밀번호.txt", True),
        ("holiday.jpg", False),
        ("report.pdf", False),
    ],
)
def test_sensitive_names(name, sensitive):
    assert is_sensitive_name(name) is sensitive


@pytest.mark.parametrize(
    "name,category",
    [
        ("a.JPG", "이미지"),
        ("a.pdf", "문서"),
        ("a.hwp", "문서"),
        ("a.mp4", "동영상"),
        ("a.mp3", "음악"),
        ("a.zip", "압축파일"),
        ("setup.exe", "프로그램"),
        ("a.py", "코드"),
        ("noext", "기타"),
        ("a.weird", "기타"),
    ],
)
def test_category_of(name, category):
    assert category_of(name) == category


def test_resolve_accepts_paths_inside_roots(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("x")
    assert resolve_in_roots(str(root / "a.txt"), [str(root)]) == (root / "a.txt").resolve()


def test_resolve_rejects_outside_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "k.txt").write_text("x")
    with pytest.raises(PermissionError):
        resolve_in_roots(str(outside / "k.txt"), [str(root)])
    with pytest.raises(PermissionError):
        resolve_in_roots(str(root / ".." / "secret" / "k.txt"), [str(root)])
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PermissionError):
        resolve_in_roots(str(link / "k.txt"), [str(root)])


def test_resolve_denies_listed_folders_even_inside_roots(tmp_path):
    root = tmp_path / "home"
    data = root / ".jarvis"
    data.mkdir(parents=True)
    with pytest.raises(PermissionError):
        resolve_in_roots(str(data), [str(root)], deny=[str(data)])


def test_resolve_maps_folder_names_to_roots(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "a.txt").write_text("x")
    assert resolve_in_roots("Downloads", [str(downloads)]) == downloads.resolve()
    assert resolve_in_roots("Downloads/a.txt", [str(downloads)]) == (downloads / "a.txt").resolve()


def test_resolve_unknown_relative_path_is_an_error(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_in_roots("nothing/here", [str(root)])


# --- tool fixtures ----------------------------------------------------------


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, target):
        self.calls.append(target)


@pytest.fixture
def env(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    data = tmp_path / "data"
    launcher, opener = Recorder(), Recorder()
    pc = PcControl(data, launcher=launcher, opener=opener, home=tmp_path)
    pc.settings.update(enabled=True, roots=[str(docs)])
    reg = ToolRegistry()
    register_pc_tools(reg, pc)
    return NS(docs=docs, data=data, pc=pc, reg=reg, launcher=launcher, opener=opener)


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def run(self, _tool, **inp):
        return self.reg.execute(_tool, inp)

    def risk(self, _tool, **inp):
        return self.reg.get(_tool).risk_for(inp)


def touch(path, text="x", age_s=0):
    path.write_text(text)
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))
    return path


# --- on/off switch ----------------------------------------------------------


def test_every_tool_refuses_when_pc_control_is_off(env):
    env.pc.settings.update(enabled=False)
    for name, args in [
        ("pc_list_folder", {"folder": str(env.docs)}),
        ("pc_search_files", {"query": "a"}),
        ("pc_organize_files", {"folder": str(env.docs)}),
        ("pc_undo_organize", {}),
        ("pc_open_app", {"name": "notepad"}),
        ("pc_open_url", {"url": "https://example.com"}),
        ("pc_open_path", {"path": str(env.docs)}),
        ("pc_delete_files", {"paths": [str(env.docs / "a.txt")]}),
    ]:
        out, is_error = env.run(name, **args)
        assert is_error and "꺼져" in out, name
    assert env.launcher.calls == [] and env.opener.calls == []


def test_tool_set_is_stable_whether_on_or_off(env):
    names = {d["name"] for d in env.reg.definitions()}
    env.pc.settings.update(enabled=False)
    assert {d["name"] for d in env.reg.definitions()} == names  # keeps the prompt cache valid


# --- risk -------------------------------------------------------------------


def test_everyday_actions_need_no_confirmation(env):
    assert env.risk("pc_list_folder", folder="x") == "safe"
    assert env.risk("pc_search_files", query="x") == "safe"
    assert env.risk("pc_organize_files", folder="x") == "safe"
    assert env.risk("pc_undo_organize") == "safe"
    assert env.risk("pc_open_app", name="메모장") == "safe"
    assert env.risk("pc_open_url", url="https://example.com") == "safe"
    assert env.risk("pc_open_path", path="x") == "safe"


def test_hard_to_undo_actions_need_confirmation(env):
    assert env.risk("pc_delete_files", paths=["a"]) == "confirm"
    assert env.risk("pc_open_app", name="some-unknown-tool") == "confirm"
    assert env.risk("pc_open_url", url="https://shop.example.com/checkout") == "confirm"
    assert env.risk("pc_open_url", url="https://accounts.google.com/") == "confirm"


def test_bad_input_means_confirm_not_safe(env):
    assert env.risk("pc_open_url", url=None) == "confirm"
    assert env.risk("pc_open_app") == "confirm"


# --- listing and search -----------------------------------------------------


def test_list_folder(env):
    touch(env.docs / "a.txt")
    (env.docs / "sub").mkdir()
    out, err = env.run("pc_list_folder", folder=str(env.docs))
    assert not err and "a.txt" in out and "sub" in out


def test_list_folder_outside_roots_is_refused(env, tmp_path):
    out, err = env.run("pc_list_folder", folder=str(tmp_path))
    assert err and "허용" in out


def test_search_finds_by_name_and_glob_and_skips_sensitive(env):
    touch(env.docs / "report-2026.pdf")
    touch(env.docs / "notes.txt")
    touch(env.docs / "passwords.txt")
    (env.docs / "deep").mkdir()
    touch(env.docs / "deep" / "report-old.pdf")
    out, err = env.run("pc_search_files", query="report")
    assert not err and "report-2026.pdf" in out and "report-old.pdf" in out
    assert "notes.txt" not in out
    out, _ = env.run("pc_search_files", query="*.txt")
    assert "notes.txt" in out and "passwords.txt" not in out


def test_search_never_looks_outside_roots(env, tmp_path):
    touch(tmp_path / "elsewhere.txt")
    out, _ = env.run("pc_search_files", query="elsewhere")
    assert "elsewhere.txt" not in out


def test_search_respects_limit(env):
    for i in range(30):
        touch(env.docs / f"f{i}.txt")
    out, _ = env.run("pc_search_files", query="f", limit=5)
    assert out.count(".txt") == 5


# --- organize ---------------------------------------------------------------


def test_organize_moves_files_into_category_folders(env):
    touch(env.docs / "a.jpg")
    touch(env.docs / "b.pdf")
    touch(env.docs / "c.mp3")
    out, err = env.run("pc_organize_files", folder=str(env.docs))
    assert not err
    assert (env.docs / "이미지" / "a.jpg").exists()
    assert (env.docs / "문서" / "b.pdf").exists()
    assert (env.docs / "음악" / "c.mp3").exists()
    assert not (env.docs / "a.jpg").exists()


def test_organize_dry_run_changes_nothing(env):
    touch(env.docs / "a.jpg")
    out, err = env.run("pc_organize_files", folder=str(env.docs), dry_run=True)
    assert not err and "a.jpg" in out
    assert (env.docs / "a.jpg").exists() and not (env.docs / "이미지").exists()


def test_organize_never_overwrites(env):
    (env.docs / "이미지").mkdir()
    touch(env.docs / "이미지" / "a.jpg", "old")
    touch(env.docs / "a.jpg", "new")
    env.run("pc_organize_files", folder=str(env.docs))
    assert (env.docs / "이미지" / "a.jpg").read_text() == "old"
    assert (env.docs / "이미지" / "a (1).jpg").read_text() == "new"


def test_organize_skips_hidden_sensitive_folders_and_unfinished_downloads(env):
    touch(env.docs / ".hidden.jpg")
    touch(env.docs / "passwords.txt")
    touch(env.docs / "movie.mp4.crdownload")
    touch(env.docs / "x.jpg")
    (env.docs / "keep").mkdir()
    env.run("pc_organize_files", folder=str(env.docs))
    assert (env.docs / ".hidden.jpg").exists()
    assert (env.docs / "passwords.txt").exists()
    assert (env.docs / "movie.mp4.crdownload").exists()
    assert (env.docs / "keep").is_dir()
    assert (env.docs / "이미지" / "x.jpg").exists()


def test_organize_is_limited_per_call(env):
    for i in range(520):
        touch(env.docs / f"n{i}.txt", "")
    out, err = env.run("pc_organize_files", folder=str(env.docs))
    assert not err and "500" in out
    assert len(list((env.docs / "문서").iterdir())) == 500


def test_undo_restores_the_last_organize(env):
    touch(env.docs / "a.jpg", "A")
    touch(env.docs / "b.pdf", "B")
    env.run("pc_organize_files", folder=str(env.docs))
    out, err = env.run("pc_undo_organize")
    assert not err
    assert (env.docs / "a.jpg").read_text() == "A" and (env.docs / "b.pdf").read_text() == "B"
    assert not (env.docs / "이미지").exists() and not (env.docs / "문서").exists()


def test_undo_does_not_clobber_a_file_that_reappeared(env):
    touch(env.docs / "a.jpg", "old")
    env.run("pc_organize_files", folder=str(env.docs))
    touch(env.docs / "a.jpg", "newer")  # the user made a new a.jpg meanwhile
    env.run("pc_undo_organize")
    assert (env.docs / "a.jpg").read_text() == "newer"
    assert (env.docs / "이미지" / "a.jpg").read_text() == "old"


def test_undo_with_nothing_to_undo(env):
    out, err = env.run("pc_undo_organize")
    assert err and "되돌릴" in out


def test_organize_outside_roots_is_refused(env, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    touch(other / "a.jpg")
    out, err = env.run("pc_organize_files", folder=str(other))
    assert err and (other / "a.jpg").exists()


# --- delete (always moves to a recoverable trash) ---------------------------


def test_delete_moves_to_trash_instead_of_erasing(env):
    f = touch(env.docs / "old.txt", "keep me")
    out, err = env.run("pc_delete_files", paths=[str(f)])
    assert not err and not f.exists()
    trashed = [p for p in (env.data / "trash").rglob("old.txt")]
    assert len(trashed) == 1 and trashed[0].read_text() == "keep me"


def test_delete_refuses_folders_outside_files_and_too_many(env, tmp_path):
    (env.docs / "dir").mkdir()
    out, err = env.run("pc_delete_files", paths=[str(env.docs / "dir")])
    assert err and (env.docs / "dir").is_dir()
    outside = touch(tmp_path / "outside.txt")
    out, err = env.run("pc_delete_files", paths=[str(outside)])
    assert err and outside.exists()
    many = [str(touch(env.docs / f"m{i}.txt")) for i in range(51)]
    out, err = env.run("pc_delete_files", paths=many)
    assert err and all(os.path.exists(p) for p in many)


def test_delete_is_all_or_nothing(env):
    good = touch(env.docs / "good.txt")
    out, err = env.run("pc_delete_files", paths=[str(good), str(env.docs / "missing.txt")])
    assert err and good.exists()


def test_jarvis_own_data_folder_is_off_limits(env):
    env.pc.settings.update(roots=[str(env.data.parent)])  # a root that happens to contain the data dir
    env.data.mkdir(exist_ok=True)
    secret = touch(env.data / "web_token", "tok")
    out, err = env.run("pc_delete_files", paths=[str(secret)])
    assert err and secret.exists()
    out, err = env.run("pc_list_folder", folder=str(env.data))
    assert err


# --- launching --------------------------------------------------------------


def test_open_known_app_by_english_or_korean_name(env):
    out, err = env.run("pc_open_app", name="메모장")
    assert not err and env.launcher.calls == ["notepad.exe"]
    env.run("pc_open_app", name="Calculator")
    assert env.launcher.calls[-1] == "calc.exe"


def test_user_added_apps_are_known(env):
    env.pc.settings.update(apps={"MyEditor": "myeditor.exe"})
    assert env.risk("pc_open_app", name="myeditor") == "safe"
    env.run("pc_open_app", name="myeditor")
    assert env.launcher.calls == ["myeditor.exe"]


def test_unknown_app_is_refused_unless_it_exists_on_the_path(env):
    out, err = env.run("pc_open_app", name="definitely-not-installed-xyz")
    assert err and env.launcher.calls == []


@pytest.mark.parametrize("name", ["C:\\Windows\\System32\\cmd.exe", "../evil", "a b && calc", "x;y", "a|b"])
def test_app_names_cannot_smuggle_paths_or_commands(env, name):
    out, err = env.run("pc_open_app", name=name)
    assert err and env.launcher.calls == []


def test_open_url_opens_web_pages_only(env):
    out, err = env.run("pc_open_url", url="https://example.com/a")
    assert not err and env.opener.calls == ["https://example.com/a"]
    out, err = env.run("pc_open_url", url="file:///etc/passwd")
    assert err and env.opener.calls == ["https://example.com/a"]


def test_open_path_opens_documents_inside_roots(env):
    f = touch(env.docs / "plan.pdf")
    out, err = env.run("pc_open_path", path=str(f))
    assert not err and env.opener.calls == [str(f.resolve())]


@pytest.mark.parametrize("name", ["setup.exe", "run.bat", "x.cmd", "s.ps1", "i.msi", "v.vbs", "a.lnk", "j.jar", "r.reg"])
def test_open_path_never_runs_programs(env, name):
    f = touch(env.docs / name)
    out, err = env.run("pc_open_path", path=str(f))
    assert err and env.opener.calls == []


def test_system_prompt_tells_the_model_how_pc_control_is_gated():
    from jarvis.prompts import SYSTEM_PROMPT

    assert "pc_*" in SYSTEM_PROMPT and "do not ask permission" in SYSTEM_PROMPT
