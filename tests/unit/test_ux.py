"""Ranges, suggestions, completion, the wizard's answers, progress output, handout escaping, the PVE check."""
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
    (["create", "2", "--script", "h"], 4, ["harden"]),
    (["create", "2", "--script", "harden+d"], 4, ["harden+docker"]),
    (["create", "2x2", "--script", "harden,d"], 4, ["harden,docker"]),
    (["create", "2x2", "--script", "harden+docker,h"], 4, ["harden+docker,harden"]),
    (["scripts", ""], 2, ["docker", "harden"]),
])
def test_completion(words, cword, expect):
    assert complete.candidates(words, cword, DATA.get) == expect


def test_completion_run_offers_library_scripts():
    assert "docker" in complete.candidates(["run", "net40-student01", "do"], 3, DATA.get)


def test_help_create_points_at_harden():
    h = command_help("create")
    assert 'only "student" may ssh in' not in h and "harden" in h and "pvelab scripts" in h
    assert command_help("scripts") and "harden+docker" in command_help("run")


def test_completion_commands_match_cli():
    from pvelab import cli
    assert set(complete.COMMANDS) == set(cli.NAMES) - {"ls"}


def test_completion_fetches_only_what_it_needs():
    asked = []
    complete.candidates(["re"], 1, lambda k: asked.append(k) or [])
    complete.candidates(["rollback", "--to", ""], 3, lambda k: asked.append(k) or [])
    assert asked == ["snaps"]


@pytest.mark.parametrize("cmd", sorted(complete.OPTIONS))
def test_completion_options_are_documented(cmd):
    assert all(o in command_help(cmd) for o in complete.OPTIONS[cmd]), cmd


@pytest.mark.parametrize("cmd", [c for c in complete.COMMANDS if c != "help"])
def test_completion_commands_have_help(cmd):
    assert command_help(cmd)


def test_vm_words():
    assert wizard.vm_words("30, net30-student01..03 net40") == ["--net", "30", "net30-student01..03", "--net", "40"]


def answers(monkeypatch, *replies):
    """Replies to typed questions and to lists (in lists: "" = the default, "b" = back, else a
    piece of the label to pick)."""
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))

    def select(prompt, labels, default=0, esc="back"):
        a = next(it)
        return default if a == "" else None if a == "b" else next(i for i, l in enumerate(labels) if a in l)
    monkeypatch.setattr(wizard, "select", select)


def test_wizard_create(monkeypatch, tmp_path, capsys):
    script = tmp_path / "setup.sh"
    script.write_text("#!/bin/sh\n")
    answers(monkeypatch, "5", "3", "n", "2604", "", "y", "y", "y", "4", "4G", "",
            f"harden+{script}", "no-such-script", "harden", "")            # scripts of VM -1, -2 (asked again), -3
    assert wizard.Wizard(DATA).create() == ["create", "5x3", "ubuntu26", "--net", "auto", "--no-isolate", "--expose",
                                            "--cores", "4", "--memory", "4G", "--script", f"harden+{script},harden,"]
    out = capsys.readouterr().out
    assert "docker" in out and "unknown script 'no-such-script'" in out    # the library is shown; typos asked again
    assert "--expose without the harden script (VM -3)" in out


def test_wizard_create_same_scripts_everywhere(monkeypatch, capsys):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    answers(monkeypatch, "2", "2", "n", "", "30", "y", "n", "harden", "harden")
    assert wizard.Wizard(DATA).create()[-2:] == ["--script", "harden"]
    assert "without the harden script" not in capsys.readouterr().out


def test_wizard_run_asks_for_a_chain(monkeypatch):
    monkeypatch.setattr(wizard, "targets", lambda a: [1])
    answers(monkeypatch, "40", "harden+nope", "harden+docker", "")
    assert wizard.Wizard(DATA).run() == ["run", "--net", "40", "harden+docker"]
    assert ("run", "Run scripts in VMs") in wizard.MENU


def test_wizard_create_existing_net_keeps_its_isolation(monkeypatch):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    answers(monkeypatch, "2", "", "", "30", "n", "n", "")
    assert wizard.Wizard(DATA).create() == ["create", "2", "debian13", "--net", "30"]


def test_wizard_rejects_then_accepts(monkeypatch, capsys):
    from pvelab.util import die
    monkeypatch.setattr(wizard, "targets", lambda a: [1] if a == ["--net", "40"] else die(f"no student VMs on {a}"))
    answers(monkeypatch, "99", "40")                     # targets() refuses net99: asked again
    assert wizard.Wizard(DATA).which_vms() == ["--net", "40"]
    assert "no student VMs on ['--net', '99']" in capsys.readouterr().out


def test_wizard_resize_previews_first(monkeypatch):
    monkeypatch.setattr(wizard, "targets", lambda a: [1])
    answers(monkeypatch, "net30-student01..02", "", "8G", "", "n")
    assert wizard.Wizard(DATA).resize() == ["resize", "net30-student01..02", "--memory", "8G"]
    from pvelab.cli import DRY_RUN
    assert {"resize", "rollback", "destroy"} <= set(DRY_RUN)              # the wizard previews these


def test_wizard_back(monkeypatch):
    monkeypatch.setattr(wizard, "targets", lambda a: [1])
    answers(monkeypatch, "b")
    with pytest.raises(wizard.Back):
        wizard.Wizard(DATA).power()


def test_board_plain_output(capsys, monkeypatch):
    from pvelab.ui import Board
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    with Board("[3/3] VMs", {1: "a", 2: "b"}) as b:
        b.stage(1, "first boot", live=True)
        b.tail(1, "Setting up x ...")                  # live lines go to the log only, not to a pipe
        b.ok(1, "ready")
        b.fail(2, "clone: no space")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "[3/3] VMs" and "a: first boot" in out[1] and "Setting up" not in "".join(out)
    assert out[-2].endswith("a: ready") and "b: clone: no space" in out[-1] and b.counts() == (1, 1, 2)


def test_board_lines_fit_a_small_window(monkeypatch):
    from pvelab import ui
    monkeypatch.setattr(ui.shutil, "get_terminal_size", lambda fallback: __import__("os").terminal_size((50, 8)))
    b = ui.Board("t", {i: f"net40-student{i:02d}" for i in range(20)})
    for i in range(20):
        b.stage(i, "first boot", live=True)
        b.tail(i, "x" * 200)
    b.ok(0, "ready")
    b.ok(1, "ready")
    b.fail(2, "exit 1")
    lines = b._lines()
    plain = [ui.ANSI.sub("", line) for line in lines]
    assert len(lines) <= 8 - 3 + 1 and all(len(p) <= 49 for p in plain)
    assert "exit 1" in plain[1] and "2 done" in plain[2] and plain[-1].strip().startswith("…")


def test_board_marks_a_stuck_vm(monkeypatch):
    from pvelab import ui
    b = ui.Board("t", {1: "a"})
    b.stage(1, "script 1/1 docker", live=True)
    b.tail(1, "Unpacking docker-ce ...")
    b.rows[1]["tail_t"] -= ui.STUCK + 5
    assert "no output for 2:" in ui.ANSI.sub("", b._lines()[1])


def test_plan_asks_once_and_names_the_settings(capsys, monkeypatch):
    from pvelab import ui
    monkeypatch.setattr(ui, "FORCE", False)
    monkeypatch.setattr(ui, "YES", True)
    p = ui.Plan()
    p.do("create 4 VM(s)")
    p.warn("local-lvm is 86% full", force=True)
    p.warn("template copied to b")
    with pytest.raises(LabError, match="LAB_FORCE=1") as e:      # no terminal: stops, naming what is missing
        p.ask()
    assert "LAB_YES" not in str(e.value)
    out = capsys.readouterr().out
    assert "1. create 4 VM(s)" in out and "! local-lvm is 86% full" in out and "! template copied to b" in out


def test_handout_escapes(monkeypatch, tmp_path):
    from pvelab.commands import handout
    monkeypatch.setattr(handout, "targets", lambda a: [201])
    monkeypatch.setattr(handout, "cred_rows", lambda: {201: {"net": "30", "user": "student", "password": "<b>&x"}})
    monkeypatch.setattr(handout, "public_host", lambda: "203.0.113.5")
    monkeypatch.setattr(handout, "is_exposed", lambda n: True)
    monkeypatch.setattr(handout, "forwarded_on", lambda nets: {201})
    monkeypatch.setattr(handout, "vm_names", lambda ids: {i: "net30-student01" for i in ids})
    out = tmp_path / "h.html"
    handout.cmd_handout(["--net", "30", "-o", str(out)])
    page = out.read_text()
    assert "&lt;b&gt;&amp;x" in page and "ssh -p 22201 student@203.0.113.5" in page
    assert out.stat().st_mode & 0o777 == 0o600


def test_students_and_isolation_table():
    from pvelab.isolation import nft_table, student_of, vms_per_student
    assert student_of("net30-student01-2") == student_of("net30-student01-10") == "net30-student01"
    assert student_of("net30-student01") == "net30-student01"
    assert vms_per_student(["net30-student01-1", "net30-student01-2", "net30-student02-1"]) == 2
    assert vms_per_student(["net30-student01", "net30-student02"]) == 1 and vms_per_student([]) == 0
    t = nft_table("30", {201: "net30-student01-1", 202: "net30-student01-2", 203: "net30-student02-1"})
    assert '"tap201i0" . "tap202i0"' in t and '"tap202i0" . "tap201i0"' in t and "tap203i0" not in t
    assert 'meta ibrname != "vmbr30" accept' in t and t.rstrip().endswith("}")
    lone = nft_table("30", {201: "net30-student01", 202: "net30-student02"})
    assert "elements" not in lone and "drop" in lone      # one VM each: no pair may talk


def test_not_pve_stops_before_sudo(monkeypatch):
    from pvelab import cli
    monkeypatch.setattr(cli.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "elevate", lambda: pytest.fail("asked for sudo"))
    with pytest.raises(LabError, match="not a Proxmox VE host"):
        cli.main(["list"])


@pytest.mark.parametrize("keys,expect", [
    (b"\x1b[B\r", "c"), (b"\x1b[A\x1b[A\r", "c"), (b"jj\r", "a"), (b"\r", "b"), (b"\x1b", "back"), (b"\x03", "back")])
def test_arrow_menu(keys, expect):
    """The real menu on a pseudo-terminal: arrows wrap around, Esc and Ctrl-C go back."""
    import os, pty, select, sys
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["PYTHONPATH"] = os.path.dirname(os.path.dirname(wizard.__file__))
        os.execvp(sys.executable, [sys.executable, "-c",
                  "from pvelab.wizard import choose, Back\n"
                  "try: print('RESULT', choose('OS', [('a', 'A'), ('b', 'B'), ('c', 'C')], default='b'))\n"
                  "except Back: print('RESULT back')"])
    out = b""
    while b"Esc" not in out:
        out += os.read(fd, 4096)
    os.write(fd, keys)
    while select.select([fd], [], [], 5)[0]:
        try:
            out += os.read(fd, 4096)
        except OSError:
            break
    os.waitpid(pid, 0)
    assert f"RESULT {expect}".encode() in out


@pytest.mark.parametrize("conf,expect", [
    (None, ["download", "create", "template"]),
    ({"template": "1", "name": "ubuntu-2404-cloud", "description": "x image=abcdef0123456789 built=2026-10-01"}, []),
    ({"template": "1", "name": "ubuntu-2404-cloud", "description": "built by lab.py from ..."},
     ["download", "destroy", "create", "template"]),
])
def test_pull_rebuilds_only_for_a_newer_image(monkeypatch, conf, expect):
    from pvelab import templates
    calls = []
    monkeypatch.setattr(templates, "read_conf", lambda v: conf)
    monkeypatch.setattr(templates, "vm_exists", lambda v: conf is not None)
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {} for v in ids})
    monkeypatch.setattr(templates, "latest_sum", lambda t: "abcdef0123456789" + "0" * 48)
    monkeypatch.setattr(templates, "fetch_image", lambda t, want: calls.append("download") or "/x.img")
    monkeypatch.setattr(templates, "run", lambda *a, **k: calls.append(a[1]))
    templates.build_template(templates.catalogue_find("ubuntu24"))
    assert [c for c in calls if c != "disk"] == expect


def test_pull_leaves_a_foreign_vmid_alone(monkeypatch):
    from pvelab import templates
    monkeypatch.setattr(templates, "read_conf", lambda v: {"name": "someones-vm"})
    with pytest.raises(LabError, match="left alone"):
        templates.build_template(templates.catalogue_find("ubuntu24"))


def test_sbin_on_path(monkeypatch, capsys):
    """qm, ifup, iptables live in /usr/sbin, which some users' (and sudo's) PATH lacks."""
    import os
    from pvelab import cli
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    cli.main(["version"])
    assert "/usr/sbin" in os.environ["PATH"].split(":")


def test_linked_clones(monkeypatch, tmp_path):
    """Volume names of linked clones per storage type; LVM-thin clones don't name their base."""
    from pvelab import templates
    disks = {201: "local-zfs:base-9003-disk-0/vm-201-disk-0,size=10G",           # ZFS
             202: "local:9003/base-9003-disk-0.qcow2/202/vm-202-disk-0.qcow2",    # directory, qcow2
             203: "ceph:base-9003-disk-1/vm-203-disk-0,size=10G",                 # Ceph RBD
             204: "local-lvm:vm-204-disk-0,size=10G",                             # LVM-thin
             205: "local-zfs:base-90031-disk-0/vm-205-disk-0"}                    # another template
    for v, d in disks.items():
        (tmp_path / f"{v}.conf").write_text(f"name: s{v}\nscsi0: {d}\n")
    monkeypatch.setattr(templates.glob, "glob", lambda pattern: sorted(map(str, tmp_path.glob("*.conf"))))
    assert templates.linked_clones(9003, 90031) == {9003: {201: "s201", 202: "s202", 203: "s203"},
                                                    90031: {205: "s205"}}


def test_pull_skips_a_template_linked_clones_need(monkeypatch, capsys):
    from pvelab import templates
    conf = {"template": "1", "name": "ubuntu-2604-cloud", "description": "built by lab.py"}
    monkeypatch.setattr(templates, "read_conf", lambda v: conf)
    monkeypatch.setattr(templates, "latest_sum", lambda t: "f" * 64)
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {201: "net30-student01"} for v in ids})
    monkeypatch.setattr(templates, "fetch_image", lambda t, w: pytest.fail("downloaded"))
    monkeypatch.setattr(templates, "run", lambda *a, **k: pytest.fail(f"ran {a}"))
    templates.build_template(templates.catalogue_find("ubuntu26"))
    assert "not updated" in capsys.readouterr().err


def test_forwarded_reads_the_expose_section(monkeypatch, tmp_path):
    from pvelab import expose
    ifaces = tmp_path / "interfaces"
    ifaces.write_text("# lab.sh net 30 begin\n        # expose net 30 begin\n"
                      + "".join(f"        post-{w} iptables -t nat {op} PREROUTING -i vmbr0 -p tcp --dport {p} -j DNAT "
                                f"--to-destination 10.10.30.11:22\n" for w, op in (("up  ", "-A"), ("down", "-D"))
                                for p in (22201, 22202))
                      + "        # expose net 30 end\n# lab.sh net 30 end\n")
    monkeypatch.setattr(expose, "IFACES", str(ifaces))
    monkeypatch.setattr("pvelab.ifaces.IFACES", str(ifaces))
    assert expose.forwarded("30") == {201, 202} and expose.forwarded("40") == set()


def test_wizard_create_per_vm_specs(monkeypatch):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    answers(monkeypatch, "10", "2", "y",
            "2604", "4", "8G", "40",                   # VM -1: ubuntu26, big
            "debian-13", "1", "2G", "",               # VM -2: debian13, small (disk: same 40 as -1)
            "30", "n", "", "")
    assert wizard.Wizard(DATA).create() == ["create", "10x2", "ubuntu26,debian13", "--net", "30",
                                            "--cores", "4,1", "--memory", "8G,2G", "--disk", "40"]


def fake_create(monkeypatch, made, checked):
    """create's host side faked: clones are recorded in made, resource checks in checked."""
    from pvelab.commands import create
    monkeypatch.setattr(create, "resolve_template", lambda t: {"ubuntu26": 9004, "debian13": 9002}[t])
    monkeypatch.setattr(create, "read_conf", lambda t: {"scsi0": "local-lvm:base-1-disk-0,size=10G"})
    monkeypatch.setattr(create, "foreign_use", lambda n: None)
    monkeypatch.setattr(create, "inventory", lambda: {})
    monkeypatch.setattr(create, "cred_rows", lambda: {})
    monkeypatch.setattr(create, "check_resources", lambda n, mem, *a: checked.append((n, mem)) or [])
    monkeypatch.setattr(create, "host_numbers", lambda hosts: {h: (None, (None, None)) for h in hosts})
    monkeypatch.setattr(create, "copy_needed", lambda t, h, inv=None, gb=None: None)
    monkeypatch.setattr(create, "template_on", lambda t, h, **kw: t)
    monkeypatch.setattr(create, "is_new_net", lambda n: False)
    monkeypatch.setattr(create, "is_isolated", lambda n: False)
    monkeypatch.setattr(create, "ensure_bridge", lambda n: None)
    monkeypatch.setattr(create, "next_ip", lambda n, rows: "10.10.95.11")
    monkeypatch.setattr(create, "refresh_net", lambda n: None)
    monkeypatch.setattr(create, "wait_first_boot", lambda ids, board: {})
    monkeypatch.setattr(create, "snapshot_all", lambda ids, board, snap, desc: None)

    def clone(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk, orig=None, board=None):
        rows[vmid] = {"name": name}
        made.append((name, tpl, cores, mem, disk))
    monkeypatch.setattr(create, "clone_vm", clone)
    monkeypatch.setattr(create, "run_and_snapshot", lambda *a, **k: made.append("ran scripts"))
    return create


def test_create_per_vm_specs(monkeypatch):
    made, checked = [], []
    create = fake_create(monkeypatch, made, checked)
    create.cmd_create(["2x2", "ubuntu26,debian13", "--net", "95", "--cores", "4,1", "--memory", "8G", "--disk", "40,",
                       "--script", "harden,"])
    assert made == [("net95-student01-1", 9004, 4, 8192, 40), ("net95-student01-2", 9002, 1, 8192, None),
                    ("net95-student02-1", 9004, 4, 8192, 40), ("net95-student02-2", 9002, 1, 8192, None)]
    assert checked == [(4, 4 * 8192)]         # no first boot (wait_first_boot: {}), so no scripts ran


def test_create_hands_chains_to_scripts(monkeypatch):
    made, checked, ran, snaps = [], [], [], []
    create = fake_create(monkeypatch, made, checked)
    monkeypatch.setattr(create, "wait_first_boot", lambda ids, board: {i: "done" for i in ids})
    monkeypatch.setattr(create, "snapshot_all", lambda ids, board, snap, desc: snaps.extend((i, snap) for i in ids))
    monkeypatch.setattr(create, "run_and_snapshot", lambda chains, **k: ran.append((chains, k)))
    create.cmd_create(["2x2", "debian13", "--net", "95", "--script", "harden,"])
    (chains, kw), = ran
    first = create.ID_START                   # clones: student01-1, -2, student02-1, -2
    assert {i: [s.name for s in c] for i, c in chains.items()} == {first: ["harden"], first + 2: ["harden"]}
    assert kw["pre_snap"] is False and kw["board"]   # 'clean' is the before snapshot; the create's own board
    assert sorted(snaps) == [(first + k, "clean") for k in range(4)]


def test_create_expose_without_harden_asks(monkeypatch, capsys):
    from pvelab import ui
    made, checked = [], []
    create = fake_create(monkeypatch, made, checked)
    monkeypatch.setattr(ui, "YES", False)
    with pytest.raises(LabError, match="LAB_YES=1"):   # off a tty, no LAB_YES=1: stops before any clone
        create.cmd_create(["2x2", "debian13", "--net", "95", "--expose", "--script", "harden,"])
    assert made == []                                   # the plan asked before anything was made
    assert "--expose without the harden script (VM -2)" in capsys.readouterr().out
    monkeypatch.setattr(ui, "YES", True)
    monkeypatch.setattr(create, "expose_net", lambda net, host: None)
    create.cmd_create(["2x2", "debian13", "--net", "95", "--expose", "--script", "harden,"])
    assert len(made) == 4 and "continuing because LAB_YES=1" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["2x3", "--net", "95", "--cores", "4,1"],      # 2 values for 3 VMs
                                  ["2", "--net", "95", "--memory", "2G,8G"],      # a list without NxM
                                  ["2x2", "--net", "95", "--disk", "40,+5"]])
def test_create_per_vm_specs_rejected(argv):
    from pvelab.commands import create
    with pytest.raises(LabError):
        create.cmd_create(argv)


def test_wizard_save_template(monkeypatch):
    monkeypatch.setattr(wizard, "inventory", lambda: {9003: {"name": "ubuntu-2604-cloud", "template": "1"}})
    monkeypatch.setattr(wizard, "resolve", lambda a: 201 if a == "net30-student01" else __import__("pvelab.util").util.die("no such VM"))
    answers(monkeypatch, "save a VM", "net30-student01", "ubuntu-2604-cloud", "docker lab", "docker-lab")
    assert wizard.Wizard(DATA).templates() == ["save-template", "net30-student01", "docker-lab"]


@pytest.mark.parametrize("name", ["docker lab", "-x", "ubuntu26", "Ubuntu-2604-Cloud", "net30-student01"])
def test_template_name_rejected(name):
    from pvelab.templates import check_template_name
    inv = {9003: {"name": "ubuntu-2604-cloud", "template": "1"}, 201: {"name": "net30-student01"}}
    with pytest.raises(LabError):
        check_template_name(name, inv)


SRC = {"_node": "workshops", "name": "net30-student01", "net0": "virtio=AA:BB,bridge=vmbr30", "scsi0": "local-lvm:vm-201-disk-0,size=10G"}


def saving(monkeypatch, fail_on=None, exc=None):
    """save_template with qm faked: fail_on = the qm subcommand that fails (with exc)."""
    from pvelab import templates
    calls, made = [], {}

    def run(*cmd, **k):
        if cmd[0] == "lvs":
            return "  vm-201-disk-0 10.00 30.00\n  vm-201-cloudinit 0.00 4.00\n"
        calls.append(cmd[1])
        if cmd[1] == fail_on:
            raise exc
        if cmd[1] == "clone":
            made.update(name=cmd[5])
        return ""
    monkeypatch.setattr(templates, "run", run)
    monkeypatch.setattr(templates, "read_conf", lambda v: SRC if v == 201 else (made or None))
    monkeypatch.setattr(templates, "vm_exists", lambda v: False)
    monkeypatch.setattr(templates, "check_copy_space", lambda gb, node=None: calls.append(f"space {gb:g}") or [])
    monkeypatch.setattr(templates, "first_boot_state", lambda v: ("done", None))
    monkeypatch.setattr(templates, "guest_json", lambda *a, **k: {"exitcode": 0})
    monkeypatch.setattr(templates, "vm_status", lambda v: "stopped")
    return templates, calls


def test_save_template_checks_space_from_the_data(monkeypatch):
    templates, calls = saving(monkeypatch)
    templates.save_template(201, "docker-lab")
    assert calls[0] == "space 3" and calls[-1] == "template"      # 30% of 10 GB; cloudinit not counted


@pytest.mark.parametrize("fail_on,exc", [("start", LabError("command failed")), ("start", KeyboardInterrupt())])
def test_save_template_removes_its_unfinished_copy(monkeypatch, fail_on, exc):
    templates, calls = saving(monkeypatch, fail_on, exc)
    with pytest.raises(type(exc)):
        templates.save_template(201, "docker-lab")
    assert calls[-1] == "destroy" and "template" not in calls


def test_copy_space(monkeypatch):
    from pvelab import resources
    monkeypatch.setattr(resources, "storage", lambda node=None: (75, 20))           # 80 GB total, 20 free
    with pytest.raises(LabError, match="only 20 GB free"):
        resources.check_copy_space(25)
    assert resources.check_copy_space(1) == []                             # 76%: fine
    assert "~81% full" in resources.check_copy_space(5)[0]


def test_save_template_removes_a_half_cloned_placeholder(monkeypatch):
    templates, calls = saving(monkeypatch, "clone", KeyboardInterrupt())   # Ctrl-C while qm clone copies
    monkeypatch.setattr(templates, "read_conf", lambda v: SRC if v == 201 else
                        {"lock": "clone", "description": " qmclone temporary file"})
    with pytest.raises(KeyboardInterrupt):
        templates.save_template(201, "docker-lab")
    assert calls[-3:] == ["unlock", "stop", "destroy"]


def test_rules_are_added_only_once():
    from pvelab.ifaces import iptables
    up, down = iptables(["-t nat POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE", "FORWARD -i lab30 -j DROP"])
    assert up == ["iptables -t nat -C POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE 2>/dev/null || "
                  "iptables -t nat -A POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE",
                  "iptables -C FORWARD -i lab30 -j DROP 2>/dev/null || iptables -A FORWARD -i lab30 -j DROP"]
    assert down[1] == "iptables -D FORWARD -i lab30 -j DROP"


def test_old_plain_rules_are_made_add_once(monkeypatch, tmp_path):
    from pvelab import ifaces
    f = tmp_path / "interfaces"
    f.write_text("        post-up   iptables -t nat -A POSTROUTING -s 10.10.10.0/24 -j MASQUERADE\n"
                 "        post-up   iptables -I FORWARD -i vmbr0 -j ACCEPT\n"
                 "        post-up   nft -f /root/lab/nft/net30.nft\n"
                 "        post-down iptables -t nat -D POSTROUTING -s 10.10.10.0/24 -j MASQUERADE\n")
    monkeypatch.setattr(ifaces, "IFACES", str(f))
    monkeypatch.setattr(ifaces, "run", lambda *a, **k: "")
    ifaces.make_rules_add_once()
    assert f.read_text().splitlines() == [
        "        post-up   iptables -t nat -C POSTROUTING -s 10.10.10.0/24 -j MASQUERADE 2>/dev/null || "
        "iptables -t nat -A POSTROUTING -s 10.10.10.0/24 -j MASQUERADE",
        "        post-up   iptables -C FORWARD -i vmbr0 -j ACCEPT 2>/dev/null || iptables -I FORWARD -i vmbr0 -j ACCEPT",
        "        post-up   nft -f /root/lab/nft/net30.nft",
        "        post-down iptables -t nat -D POSTROUTING -s 10.10.10.0/24 -j MASQUERADE"]


@pytest.mark.parametrize("ifaces,sdn,expect", [
    ("iface vmbr30 inet static\n", True, "vmbr30"),     # a net made before SDN (or after a fallback) stays a bridge
    ("iface lab30 inet static\n", False, "lab30"),      # an SDN net stays SDN
    ("", True, "lab30"),                                 # a new net: SDN where it works
    ("", False, "vmbr30"),                               # ... else a bridge, as before
])
def test_bridge_of_a_net(monkeypatch, ifaces, sdn, expect):
    from pvelab import config
    monkeypatch.setattr(config, "read_text", lambda p: ifaces)
    monkeypatch.setattr(config, "sdn_available", lambda: sdn)
    assert config.bridge("30") == expect and config.is_sdn("30") == (expect == "lab30")


def test_sdn_needs_vxlan_ifupdown2_and_the_source_line(monkeypatch):
    from pvelab import config
    monkeypatch.setattr(config.os.path, "exists", lambda p: True)
    monkeypatch.setattr(config.shutil, "which", lambda c: "/usr/sbin/ifreload")
    monkeypatch.setattr(config, "read_text", lambda p: "source /etc/network/interfaces.d/*\n")
    assert config.sdn_available()
    monkeypatch.setattr(config, "read_text", lambda p: "auto lo\n")
    assert not config.sdn_available()                        # SDN's file would never be read
    monkeypatch.setattr(config, "read_text", lambda p: "source /etc/network/interfaces.d/*\n")
    monkeypatch.setattr(config, "SDN", "0")
    assert not config.sdn_available()                        # LAB_SDN=0


def test_sdn_failure_falls_back_to_a_bridge(monkeypatch):
    from pvelab import networks
    made = []
    monkeypatch.setattr(networks, "bridge", lambda n: "lab95")
    monkeypatch.setattr(networks, "is_sdn", lambda n: True)
    monkeypatch.setattr(networks, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(networks, "check_free", lambda br, sub: None)
    monkeypatch.setattr(networks, "make_sdn_net", lambda n, br, sub: networks.die("pvesh: no SDN here"))
    monkeypatch.setattr(networks, "add_net_block", lambda n, body: made.append(body))
    monkeypatch.setattr(networks, "run", lambda *a, **k: made.append(a))
    monkeypatch.setattr(networks, "warn", lambda m: made.append(m))
    networks.ensure_bridge("95")
    assert "using a bridge vmbr95" in made[0] and "auto vmbr95\niface vmbr95" in made[1] and made[2] == ("ifup", "vmbr95")


# --- several PVE hosts: completion, the wizard's "Where" and Nodes, node help

NODE_DATA = dict(DATA, nodes=["pve2", "workshops"])


@pytest.mark.parametrize("words,cword,expect", [
    (["node", ""], 2, ["list", "add", "upgrade", "rename", "-v", "-j"]),
    (["nodes", "a"], 2, ["add"]),
    (["node", "add", ""], 3, []),
    (["create", "2", "--node", ""], 4, ["pve2", "workshops", "auto"]),
    (["create", "2", "--no"], 3, ["--no-isolate", "--node"]),
])
def test_completion_nodes(words, cword, expect):
    assert complete.candidates(words, cword, NODE_DATA.get) == expect


def two_hosts(monkeypatch, pin=None, have=("workshops",)):
    """The wizard sees hosts workshops and pve2; debian-13-cloud (9002, ~2.1 GB) is on have."""
    monkeypatch.setattr(wizard, "multi", lambda: True)
    monkeypatch.setattr(wizard, "bridge_host", lambda net: pin)
    monkeypatch.setattr(wizard, "node_room", lambda: {"workshops": {"mem": 8192, "disk": 30},
                                                      "pve2": {"mem": 65536, "disk": 400}})
    inv = {9002: {"name": "debian-13-cloud", "template": "1", "_node": "workshops"}}
    monkeypatch.setattr(wizard, "inventory", lambda: inv)
    monkeypatch.setattr(wizard, "node_templates", lambda inv: {9002: "debian-13-cloud"})
    monkeypatch.setattr(wizard, "copy_needed", lambda t, h, inv=None, gb=None: None if h in have else 2.1)


@pytest.mark.parametrize("where,expect", [("", "auto"), ("all on pve2", "pve2")])
def test_wizard_create_asks_where(monkeypatch, capsys, where, expect):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    two_hosts(monkeypatch)
    answers(monkeypatch, "2", "", "", "30", "n", "n", "", where)
    assert wizard.Wizard(DATA).create() == ["create", "2", "debian13", "--net", "30", "--node", expect]
    out = capsys.readouterr().out
    assert "STUDENTS THAT FIT" in out
    rows = {l.split()[0]: l.split()[-1] for l in out.splitlines() if l.strip().startswith(("pve2", "workshops"))}
    assert rows == {"pve2": "32", "workshops": "4"}            # 2048 MB each: memory is what limits
    assert "debian-13-cloud is copied to pve2 first (~2.1 GB)" in out


def test_wizard_create_no_copy_note_when_the_host_has_it(monkeypatch, capsys):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    two_hosts(monkeypatch, have=("workshops", "pve2"))
    answers(monkeypatch, "2", "", "", "30", "n", "n", "", "")
    assert wizard.Wizard(DATA).create()[-2:] == ["--node", "auto"]
    assert "is copied to" not in capsys.readouterr().out


def test_wizard_create_pinned_net_asks_nothing(monkeypatch, capsys):
    monkeypatch.setattr(wizard, "is_new_net", lambda n: n != "30")
    two_hosts(monkeypatch, pin="workshops")
    answers(monkeypatch, "2", "", "", "30", "n", "n", "")
    assert wizard.Wizard(DATA).create() == ["create", "2", "debian13", "--net", "30"]
    assert "net30 is on workshops only" in capsys.readouterr().out


def test_wizard_nodes(monkeypatch, capsys):
    monkeypatch.setattr(wizard, "node_list", lambda: print("HOST STATUS"))
    answers(monkeypatch, "add a PVE host", "10.0.0", "10.0.0.229")
    assert wizard.Wizard(DATA).nodes() == ["node", "add", "10.0.0.229"]
    out = capsys.readouterr().out
    assert "HOST STATUS" in out and "IPv4 address, like 10.0.0.229" in out
    answers(monkeypatch, "back")
    with pytest.raises(wizard.Back):
        wizard.Wizard(DATA).nodes()
    assert ("nodes", "Nodes: add a PVE host, or see each host's free space") in wizard.MENU


def test_help_node_and_create():
    h = command_help("node")
    assert "pvecm expected 1" in h and "UDP 4789" in h and "5405-5412" in h and command_help("nodes") == h
    assert "--node auto|NAME" in command_help("create")
