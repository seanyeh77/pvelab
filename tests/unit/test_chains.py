"""Script chains: parsing, per-VM lists, file vs library, snapshot names, running, the expose warning."""
import pytest

from pvelab.ui import Board

from pvelab import scripts
from pvelab.commands.create import per_vm
from pvelab.config import PRE
from pvelab.scripts import Script, chain_snapshot_name, expose_warning, parse_chain, resolve_entry
from pvelab.util import LabError


def test_parse_chain():
    h, d = parse_chain("harden+docker")
    assert (h.name, h.source, h.label, h.base) == ("harden", "library", "harden (library)", "harden.sh")
    assert (d.name, d.source, d.label) == ("docker", "library", "docker (library)")
    assert parse_chain(" harden + docker ") == (h, d)
    assert parse_chain("") == ()
    for bad in ("harden++docker", "+harden", "harden+"):
        with pytest.raises(LabError):
            parse_chain(bad)


def test_parse_chain_file_with_plus(tmp_path):
    f = tmp_path / "a+b.sh"
    f.write_text("#!/bin/sh\n")
    (s,) = parse_chain(str(f))
    assert s.source == "file" and s.name == "a-b"


def test_per_vm_chains():
    (h, d), none = per_vm("--script", "harden+docker,", 2, parse_chain)
    assert (h.name, d.name, none) == ("harden", "docker", ())
    assert per_vm("--script", "harden", 2, parse_chain) == [(h,), (h,)]
    with pytest.raises(LabError):
        per_vm("--script", "harden,docker,", 2, parse_chain)


def test_file_wins_over_library(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harden").write_text("#!/bin/sh\necho mine\n")
    s = resolve_entry("harden")
    assert s.source == "file" and s.label == "harden (file)" and s.data == b"#!/bin/sh\necho mine\n"
    with pytest.raises(LabError) as e:
        resolve_entry("nosuch")
    assert "docker, harden" in str(e.value) and "pvelab scripts" in str(e.value)
    (tmp_path / "plain.sh").write_text("echo no shebang\n")
    with pytest.raises(LabError):
        resolve_entry("plain.sh")


def named(*names):
    return tuple(Script(n, f"{n}.sh", b"#!/bin/sh\n", n, "file") for n in names)


def test_chain_snapshot_name():
    assert chain_snapshot_name(parse_chain("harden+docker")) == "harden-docker"
    long1, long2 = named("a" * 20, "b" * 20, "c" * 20), named("a" * 20, "b" * 20, "d" * 20)
    n1 = chain_snapshot_name(long1)
    assert len(PRE + n1) <= 40 and n1 == chain_snapshot_name(long1)
    assert n1 != chain_snapshot_name(long2)
    for bad in ("clean", "initial", "current", "before-x", "x"):
        with pytest.raises(LabError):
            chain_snapshot_name(named(bad))


def test_chain_stops_on_failure(tmp_path, monkeypatch):
    a, b = Script("a", "a.sh", b"#!a", "a", "file"), Script("b", "b.sh", b"#!b", "b", "file")
    exits = {(1, b"#!a"): 0, (1, b"#!b"): 0, (2, b"#!a"): 1, (2, b"#!b"): 0}
    ran, snaps = [], []

    def guest(*cmd, data=None, timeout=60, node=None):
        ran.append((cmd[1], data))
        return {"exited": 1, "exitcode": exits[cmd[1], data], "out-data": "ok"}
    monkeypatch.setattr(scripts, "vm_statuses", lambda ids: {i: "running" for i in ids})
    monkeypatch.setattr(scripts, "agent_up", lambda i, node=None: True)
    monkeypatch.setattr(scripts, "guest_json", guest)
    monkeypatch.setattr(scripts, "take_snapshot", lambda i, snap, desc: snaps.append((i, snap)))
    monkeypatch.setattr(scripts, "vm_names", lambda ids: {i: f"vm{i}" for i in ids})
    monkeypatch.setattr(scripts, "RUN_LOG_DIR", str(tmp_path))
    failed = scripts.run_and_snapshot({1: (a, b), 2: (a, b)}, pre_snap=True)
    assert failed == {2: (a, 1)}
    assert (2, b"#!b") not in ran and (1, b"#!b") in ran
    assert sorted(snaps) == [(1, "a-b"), (1, "before-a-b"), (2, "before-a-b")]
    (logdir,) = tmp_path.iterdir()
    assert logdir.name.endswith("-a-b")
    assert (logdir / "vm1" / "1-a.log").exists() and (logdir / "vm1" / "2-b.log").exists()
    assert not (logdir / "vm2" / "2-b.log").exists()


def test_poll_one_agent_call_then_exec_status(tmp_path, monkeypatch):
    """While a script runs, each poll is one agent call (its output's tail, is it gone); exec-status
    only once it is gone, for the exit code and the whole output. A VM that stops answering waits."""
    a, = named("a")
    calls, tails = [], {1: ["one\n", "two\n\n" + scripts.EXITED + "\n"], 2: [None, None]}

    def guest(*cmd, data=None, timeout=60, node=None):
        calls.append(cmd[:2])
        return {"pid": 7} if cmd[0] == "exec" else {"exited": 1, "exitcode": 3, "out-data": "all of it"}
    monkeypatch.setattr(scripts, "guest_json", guest)
    monkeypatch.setattr(scripts, "agent_out", lambda i, *cmd, **kw: (tails[i] or [None]).pop(0))
    now = [0]                             # each poll's sleep: 25 s, so 1 min is 3 polls
    monkeypatch.setattr(scripts.time, "sleep", lambda s: now.__setitem__(0, now[0] + 25))
    monkeypatch.setattr(scripts.time, "time", lambda: now[0])
    with Board("t", {1: "vm1", 2: "vm2"}) as b:
        codes = scripts.start_and_wait({1: a, 2: a}, b, 1, str(tmp_path), 0, {1: 1, 2: 1})
    assert codes == {1: 3, 2: None}
    assert calls.count(("exec-status", 1)) == 1 and ("exec-status", 2) not in calls
    assert "all of it" in (tmp_path / "vm1" / "1-a.log").read_text()
    assert "exit 3" in b.rows[1]["why"] and "still running after 1 min" in b.rows[2]["why"]


def test_expose_warning():
    h, d = parse_chain("harden"), parse_chain("docker")
    assert expose_warning([h + d, h], 2) == []
    assert "VM -2" in expose_warning(per_vm("--script", "harden,", 2, parse_chain), 2)[0]
    assert "all VMs" in expose_warning([(), ()], 2)[0]
    assert "all VMs" in expose_warning([d], 1)[0]


def test_run_chains_skips_vms_not_ready(tmp_path, monkeypatch):
    a, = named("a")
    ran, snaps = [], []
    monkeypatch.setattr(scripts, "vm_statuses", lambda ids: {1: "running", 2: "stopped"})
    monkeypatch.setattr(scripts, "agent_up", lambda i, node=None: True)
    monkeypatch.setattr(scripts, "guest_json", lambda *cmd, data=None, timeout=60, node=None:
                        ran.append(cmd[1]) or {"exited": 1, "exitcode": 0, "out-data": ""})
    monkeypatch.setattr(scripts, "take_snapshot", lambda i, snap, desc: snaps.append((i, snap)))
    monkeypatch.setattr(scripts, "RUN_LOG_DIR", str(tmp_path))
    with Board("t", {1: "vm1", 2: "vm2"}) as b:
        result, ready, _ = scripts.run_chains({1: (a,), 2: (a,)}, {1: "a", 2: "a"}, b, pre_snap=True)
    assert result == {1: None, 2: (a, None)} and ready == [1]
    assert b.rows[2]["state"] == "fail" and "not running" in b.rows[2]["why"]
    assert 2 not in ran and (2, "before-a") not in snaps and (1, "before-a") in snaps


def test_cmd_run(monkeypatch):
    from pvelab.commands import run
    got = []
    monkeypatch.setattr(run, "targets", lambda vms: [101, 102])
    monkeypatch.setattr(run, "run_and_snapshot", lambda chains, timeout, pre_snap: got.append(
        (chains, timeout, pre_snap)) or {})
    run.cmd_run(["net40", "--timeout", "5", "harden+docker"])
    (chains, timeout, pre_snap), = got
    assert set(chains) == {101, 102} and chains[101] == chains[102] == parse_chain("harden+docker")
    assert (timeout, pre_snap) == (5, True)
    monkeypatch.setattr(run, "run_and_snapshot", lambda *a, **k: {101: (chains[101][0], 1)})
    with pytest.raises(SystemExit) as e:      # a failed script: exit status 1
        run.cmd_run(["net40", "harden"])
    assert e.value.code == 1
    with pytest.raises(LabError):             # an empty chain
        run.cmd_run(["net40", ""])


def test_first_boot_state_is_one_agent_call(monkeypatch):
    from pvelab import lifecycle
    calls = []
    answers = {1: None, 2: "status: done\n\n==\nline one\nCloud-init finished\n"}
    monkeypatch.setattr(lifecycle, "agent_out", lambda i, *cmd, **kw: calls.append((i, kw.get("node"))) or answers[i])
    assert lifecycle.first_boot_state(1) == (None, None)                 # no agent yet
    assert lifecycle.first_boot_state(2, "b") == ("done", "Cloud-init finished")
    assert calls == [(1, None), (2, "b")]
