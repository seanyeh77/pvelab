"""pvelab move: which VMs (a student's go together), what it refuses, the plan, and the steps per VM."""
import json
import os

import pytest

from pvelab import credentials, inventory, resources, wizard
from pvelab.commands import complete, move
from pvelab.helptext import UsageError
from pvelab.util import LabError
from test_ux import DATA, answers

A = os.uname().nodename.split(".")[0]          # this host
B = "pve2"


def cluster(pve, b_online=True):
    (pve / ".members").write_text(json.dumps({"nodename": A, "version": 4, "nodelist": {
        A: {"id": 1, "online": 1, "ip": "10.0.0.123"}, B: {"id": 2, "online": int(b_online), "ip": "10.0.0.229"}}}))


def vm(pve, node, vmid, name, net, snaps=("initial", "clean"), managed=True):
    d = pve / "nodes" / node / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    head = f"#managed-by=lab.sh template=9002 net={net}\n" if managed else ""
    text = head + f"name: {name}\ntags: lab-student\nmemory: 2048\nscsi0: local-lvm:vm-{vmid}-disk-0,size=10G\n"
    text += "".join(f"\n[{s}]\nname: {name}\n" for s in snaps) + "\n[special:cloudinit]\nname: x\n"
    (d / f"{vmid}.conf").write_text(text)


@pytest.fixture
def lab(empty_pve, tmp_path, monkeypatch):
    """Two hosts. net95 (SDN): student01 (201, 202) and student03 (205) on A, student02 (203, 204) on B;
    net40 (a bridge on A): 301."""
    cluster(empty_pve)
    for vmid, name in ((201, "net95-student01-1"), (202, "net95-student01-2"), (205, "net95-student03-1")):
        vm(empty_pve, A, vmid, name, "95")
    for vmid, name in ((203, "net95-student02-1"), (204, "net95-student02-2")):
        vm(empty_pve, B, vmid, name, "95")
    vm(empty_pve, A, 301, "net40-student01", "40")
    cred = tmp_path / "credentials.tsv"
    cred.write_text("".join(f"{i}\t{n}\tstudent\tpw\t{net}\t10.10.{net}.{i % 100}\n" for i, n, net in (
        (201, "net95-student01-1", 95), (202, "net95-student01-2", 95), (203, "net95-student02-1", 95),
        (204, "net95-student02-2", 95), (205, "net95-student03-1", 95), (301, "net40-student01", 40))))
    monkeypatch.setattr(credentials, "CRED", str(cred))
    monkeypatch.setattr(move, "bridge_host", lambda net: None if net == "95" else A)
    return empty_pve


def test_snapshot_names_skip_pending_and_special(lab):
    assert inventory.snapshot_names(201) == ["initial", "clean"]


def test_selection_adds_the_rest_of_a_student(lab):
    ids, added, there, _ = move.selection(["net95-student01-1"], B)
    assert ids == [201, 202] and added == ["net95-student01-2"] and there == []
    ids, added, there, _ = move.selection(["--net", "95"], B)
    assert ids == [201, 202, 205] and added == [] and there == ["net95-student02-1", "net95-student02-2"]
    ids, _, there, _ = move.selection(["net95-student02-2"], B)          # already there: nothing to move
    assert ids == [] and there == ["net95-student02-1", "net95-student02-2"]


def test_refuses_bridge_nets(lab):
    with pytest.raises(LabError) as e:
        move.cmd_move(["net40-student01", "net95-student01-1", "--to", B], dry=True)
    assert "net40-student01 (net40)" in str(e.value) and "pvelab move --net 95 --to pve2" in str(e.value)


@pytest.mark.parametrize("argv,why", [
    (["net95-student01-1", "--to", "pve3"], "--to pve3: no such host"),
    (["net95-student01-1", "--to", "pve"], "did you mean pve2"),
    (["net95-student01-1"], "--to HOST"),
    (["--to", B], "which VM"),
])
def test_refuses_bad_targets_and_usage(lab, argv, why):
    with pytest.raises((LabError, UsageError)) as e:
        move.cmd_move(argv, dry=True)
    assert why in str(e.value)


def test_refuses_an_offline_target_and_vms_pvelab_did_not_make(lab):
    cluster(lab, b_online=False)
    with pytest.raises(LabError, match="that host is offline"):
        move.cmd_move(["net95-student01-1", "--to", B], dry=True)
    cluster(lab)
    vm(lab, A, 400, "someones-vm", "95", managed=False)
    with pytest.raises(LabError, match="not created by lab"):
        move.cmd_move(["someones-vm", "--to", B], dry=True)


def looks(monkeypatch, free=100, running=(201,)):
    """What the plan reads: status, disk data (2.5 GB each), who is logged in, the target's free space."""
    monkeypatch.setattr(move, "vm_statuses", lambda ids: {i: "running" if i in running else "stopped" for i in ids})
    monkeypatch.setattr(move, "data_gb", lambda i, conf: 2.5)
    monkeypatch.setattr(move, "logged_in", lambda ids, names: ({names[i]: ["student"] for i in ids}, []))
    monkeypatch.setattr(move, "host_mem_mb", lambda node=None: (64000, 60000))
    monkeypatch.setattr(resources, "storage", lambda node=None: (40, free))


def test_plan_names_snapshots_space_and_who(lab, monkeypatch, capsys):
    looks(monkeypatch)
    move.cmd_move(["net95-student01-2", "--to", B], dry=True)
    out = capsys.readouterr().out
    assert "added, so each student's VMs stay together: net95-student01-1" in out
    assert "move 2 VM(s) of 1 student(s) to pve2" in out
    assert "net95-student01-1        initial, clean" in out and "special" not in out
    assert "~5.0 GB more on local-lvm on pve2" in out and "linked clones" in out
    assert "shut down the 1 running VM(s)" in out and "net95-student01-1: student logged in now" in out
    assert "new 'clean' snapshot" in out and "dry run - nothing changed" in out


def test_plan_refuses_when_the_disks_do_not_fit(lab, monkeypatch):
    looks(monkeypatch, free=4)
    with pytest.raises(LabError, match="needs ~5.0 GB, only 4 GB free"):
        move.cmd_move(["net95-student01-1", "--to", B], dry=True)


def fake_hosts(monkeypatch, lab, fail=()):
    """Every host command recorded as (host, words); `qm migrate` moves the config (as PVE does), or
    fails for the VMIDs in fail."""
    looks(monkeypatch)
    done = []
    monkeypatch.setattr(move, "confirm_yes", lambda msg: done.append(("ask", msg)))
    monkeypatch.setattr(move, "run", lambda *cmd, **k: done.append(("run", " ".join(map(str, cmd)))))

    def shell(node, script, timeout=60, show=None, **k):
        done.append(("shell", node, script))
        vmid = int(script.split()[2])
        if vmid in fail:
            return 1, "migration aborted\n"
        show(f"migration finished successfully (VM {vmid})")
        src = lab / "nodes" / node / "qemu-server" / f"{vmid}.conf"
        dst = lab / "nodes" / B / "qemu-server" / f"{vmid}.conf"
        src.rename(dst)
        return 0, ""
    monkeypatch.setattr(move, "shell", shell)
    monkeypatch.setattr(move, "agent_up", lambda i, node=None: True)
    monkeypatch.setattr(move, "take_snapshot", lambda i, snap, desc: done.append(("snapshot", i, snap)))
    for f in ("ensure_snippet", "ensure_gw_mac", "refresh_net"):
        monkeypatch.setattr(move, f, lambda *a, f=f: done.append((f, *a)))
    monkeypatch.setattr(move, "wait_vnet", lambda node, n: True)
    return done


def test_steps_in_order(lab, monkeypatch, capsys):
    done = fake_hosts(monkeypatch, lab)
    move.cmd_move(["net95-student01-1", "--to", B])
    steps = [d for d in done if d[0] != "ask"]
    assert steps[:7] == [
        ("ensure_snippet", B), ("ensure_gw_mac", "95"), ("refresh_net", "95"),
        ("run", "qm shutdown 201 --timeout 120 --forceStop 1"),           # 201 runs: shut down first
        ("run", "qm delsnapshot 201 clean"), ("run", "qm delsnapshot 201 initial"),
        ("shell", A, "qm migrate 201 pve2")]
    assert steps[7][1].endswith("qm start 201") and "root@10.0.0.229" in steps[7][1]    # started on pve2
    assert steps[8:] == [
        ("snapshot", 201, "clean"),
        ("run", "qm delsnapshot 202 clean"), ("run", "qm delsnapshot 202 initial"),   # 202 is stopped
        ("shell", A, "qm migrate 202 pve2"),
        ("snapshot", 202, "clean"),
        ("refresh_net", "95")]
    out = capsys.readouterr().out
    assert "migrate to pve2" in out and "net95-student01-1: on pve2, running" in out
    assert "moved 2/2 VM(s) to pve2" in out and "pvelab list --net 95" in out


def test_a_failure_stops_that_student_only(lab, monkeypatch, capsys):
    done = fake_hosts(monkeypatch, lab, fail=(201,))
    move.cmd_move(["--net", "95", "--to", B])
    migrated = [d[2] for d in done if d[0] == "shell"]
    assert migrated == ["qm migrate 201 pve2", "qm migrate 205 pve2"]           # not 202: same student as 201
    assert not any("202" in str(d) for d in done if d[0] in ("run", "snapshot"))
    out = capsys.readouterr().out
    assert "migrate: migration aborted - still on " + A in out and "start it: pvelab start net95-student01-1" in out
    assert "not moved: net95-student01-1 of the same student failed" in out
    assert "moved 1/3 VM(s) to pve2" in out and "skipped (already there): 2" in out


def test_a_split_student_is_named(lab, monkeypatch, capsys):
    fake_hosts(monkeypatch, lab, fail=(202,))
    move.cmd_move(["net95-student01-1", "--to", B])
    assert "on two hosts now: net95-student01 - once the problem is fixed, move the rest: " \
           "pvelab move 'net95-student01*' --to pve2" in capsys.readouterr().out


def test_completion_and_wizard(monkeypatch):
    data = dict(DATA, nodes=[A, B])
    assert complete.candidates(["move", "net95-student01", "--to", ""], 4, data.get) == [A, B]
    assert "--to" in complete.candidates(["move", "--"], 2, data.get)
    monkeypatch.setattr(wizard, "multi", lambda: False)
    assert "move" not in [k for k, _ in wizard.menu()]
    monkeypatch.setattr(wizard, "multi", lambda: True)
    assert ("move", "Move VMs to another host") in wizard.menu()
    monkeypatch.setattr(wizard, "online_nodes", lambda: [A, B])
    answers(monkeypatch, "40", B)
    monkeypatch.setattr(wizard, "targets", lambda args: [1])
    assert wizard.Wizard(DATA).move() == ["move", "--net", "40", "--to", B]
