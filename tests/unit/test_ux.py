"""Ranges, suggestions, completion, the wizard's answers, progress output, handout escaping."""
import pytest

from pvelab import wizard
from pvelab.commands import complete
from pvelab.helptext import command_help
from pvelab.inventory import did_you_mean, expand_range
from pvelab.util import LabError

DATA = {"vms": ["net30-student01", "net30-student02", "net40-student01"], "nets": ["30", "40"],
        "snaps": ["clean", "setup"], "count": {"30": 2, "40": 1}}


def test_expand_range():
    assert expand_range("net30-student08..10") == ["net30-student08", "net30-student09", "net30-student10"]
    assert expand_range("net30-student01..net30-student02") == ["net30-student01", "net30-student02"]
    assert expand_range("net30-student01") is None
    with pytest.raises(LabError):
        expand_range("net30-student05..01")


def test_did_you_mean():
    assert "net30-student05" in did_you_mean("net30-studnet05", ["net30-student05", "x"])
    assert did_you_mean("zzz", ["net30-student05"]) == ""


@pytest.mark.parametrize("words,cword,expect", [
    (["re"], 1, ["reboot", "resize"]),
    (["rollback", "--to", ""], 3, ["clean", "setup"]),
    (["create", "3", "--net", ""], 4, ["30", "40", "auto"]),
    (["list", "--net", "=", ""], 4, ["30", "40", "dhcp"]),
    (["block", ""], 2, ["internet", "ssh", "all"]),
    (["start", "net40"], 2, ["net40-student01"]),
    (["run", "--timeout", ""], 3, []),
])
def test_completion(words, cword, expect):
    assert complete.candidates(words, cword, DATA) == expect


@pytest.mark.parametrize("cmd", sorted(complete.OPTIONS))
def test_completion_options_are_documented(cmd):
    assert all(o in command_help(cmd) for o in complete.OPTIONS[cmd]), cmd


@pytest.mark.parametrize("cmd", [c for c in complete.COMMANDS if c != "help"])
def test_completion_commands_have_help(cmd):
    assert command_help(cmd)


def test_vm_words():
    assert wizard.vm_words("30, net30-student01..03 net40") == ["--net", "30", "net30-student01..03", "--net", "40"]


def answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_wizard_create(monkeypatch, tmp_path):
    script = tmp_path / "setup.sh"
    script.write_text("#!/bin/sh\n")
    answers(monkeypatch, "5", "ubuntu26", "", "", "y", "4", "4G", "", str(script))
    assert wizard.Wizard(DATA).create() == ["create", "5", "ubuntu26", "--net", "auto", "--expose",
                                            "--cores", "4", "--memory", "4G", "--script", str(script)]


def test_wizard_rejects_then_accepts(monkeypatch, capsys):
    answers(monkeypatch, "99", "40")                     # no net99: asked again
    assert wizard.Wizard(DATA).which_vms() == ["--net", "40"]
    assert "no net99" in capsys.readouterr().out


def test_wizard_resize_previews_first(monkeypatch):
    answers(monkeypatch, "net30-student01..02", "", "8G", "", "n")
    preview, real = wizard.Wizard(DATA).resize()
    assert real == ["resize", "net30-student01..02", "--memory", "8G"] and preview == real + ["--dry-run"]


def test_wizard_back(monkeypatch):
    answers(monkeypatch, "b")
    with pytest.raises(wizard.Back):
        wizard.Wizard(DATA).power()


def test_progress_plain_output(capsys, monkeypatch):
    from pvelab.ui import Progress
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    p = Progress("setup.sh", 3)
    p.update(0, running=["a", "b", "c"])
    p.update(0, running=["a", "b"])                      # same counts: no new line in a log
    p.update(2, 1)
    p.finish()
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "0/3 done, 3 running (a, b, c)" in out[0] and "2/3 done, 1 failed" in out[1]


def test_handout_escapes(monkeypatch, tmp_path):
    from pvelab.commands import handout
    monkeypatch.setattr(handout, "targets", lambda a: [201])
    monkeypatch.setattr(handout, "cred_rows", lambda: {201: {"net": "30", "user": "student", "password": "<b>&x"}})
    monkeypatch.setattr(handout, "public_host", lambda: "203.0.113.5")
    monkeypatch.setattr(handout, "is_exposed", lambda n: True)
    monkeypatch.setattr(handout, "read_conf", lambda i: {"name": "net30-student01"})
    out = tmp_path / "h.html"
    handout.cmd_handout(["--net", "30", "-o", str(out)])
    page = out.read_text()
    assert "&lt;b&gt;&amp;x" in page and "ssh -p 22201 student@203.0.113.5" in page
    assert out.stat().st_mode & 0o777 == 0o600
