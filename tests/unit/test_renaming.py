"""pvelab node rename: the text edits, the checks, the plan, both ways of renaming, resume, recovery
(every host command mocked: nothing runs)."""
import json
import os

import pytest

from pvelab import guest, joining, nodes, renaming, ui
from pvelab.guest import users_in
from pvelab.commands import complete
from pvelab.commands import node as node_cmd
from pvelab.util import LabError

from test_nodes import A, B, cluster, vm

HOSTS = f"127.0.0.1 localhost\n10.0.0.123 {A}.lab {A}\n# {A} stays in comments\n10.0.0.229 b.lab b\n"
COROSYNC = f"""totem {{
  cluster_name: pvelab
  config_version: 2
}}
nodelist {{
  node {{
    name: {A}
    nodeid: 1
    ring0_addr: 10.0.0.123
  }}
  node {{
    name: b
    nodeid: 2
    ring0_addr: 10.0.0.229
  }}
}}
"""
STUDENT = "name: net30-student01\ntags: lab-student\nonboot: 1\n#managed-by=lab.sh\n"
COPY = "name: debian-13-cloud\ntemplate: 1\n#copy of 9002 debian-13-cloud image=abcd\n"


# --- pure helpers ---

def test_names():
    assert renaming.bad_name("workshop1") is None and renaming.bad_name("ws-2") is None
    for bad in ("", "-ws", "ws-", "ws.lab", "ws_1", "x" * 64):
        assert renaming.bad_name(bad)


def test_hosts_and_hostname_keep_the_domain():
    out = renaming.swap(HOSTS, A, "workshop1")
    assert "10.0.0.123 workshop1.lab workshop1\n" in out and f"# {A} stays in comments" in out
    assert "10.0.0.229 b.lab b" in out and renaming.swap(HOSTS, "nothere", "x") == HOSTS
    assert renaming.swap("10.0.0.1 ws ws-backup\n", "ws", "w1") == "10.0.0.1 w1 ws-backup\n"
    assert renaming.new_hostname("workshops\n", "workshops", "workshop1") == "workshop1\n"
    assert renaming.new_hostname("workshops.lab\n", "workshops", "workshop1") == "workshop1.lab\n"


def test_corosync_rename_bumps_the_version():
    out = renaming.rename_corosync(COROSYNC, "b", "workshop2")
    assert "name: workshop2\n" in out and "    name: b\n" not in out and "cluster_name: pvelab" in out
    assert renaming.corosync_version(out) == 3 and renaming.corosync_version(COROSYNC) == 2
    assert "ring0_addr: 10.0.0.229" in renaming.node_block(COROSYNC, "b")
    with pytest.raises(LabError, match="expected one"):
        renaming.rename_corosync(COROSYNC, "nothere", "x")


def test_parsers():
    who = "student  pts/0  2026-10-31 10:00 (10.0.0.9)\nstudent  pts/1  2026-10-31 10:02\nroot tty1 x\n"
    assert users_in(who) == ["root", "student"] and users_in("") == []
    table = ("Membership information\n----------------------\n    Nodeid      Votes Name\n"
             "         1          1 workshop1 (local)\n         2          1 b\n")
    assert renaming.parse_pvecm_nodes(table) == ["workshop1", "b"]


def test_separation_follows_pve_docs_and_forget_is_guarded():
    s = renaming.SEPARATE_SH
    order = ["systemctl stop pve-cluster corosync", "pmxcfs -l", "rm -f /etc/pve/corosync.conf", "rm -rf /etc/corosync/*",
             "killall pmxcfs", "systemctl start pve-cluster", "rm -f /var/lib/corosync/*"]
    assert [s.index(o) for o in order] == sorted(s.index(o) for o in order)
    f = renaming.FORGET_SH.format(old="b")
    assert f.index("/etc/pve/corosync.conf") < f.index("rm -rf") and "is-active --quiet corosync" in f
    assert '!= /etc/pve/nodes/b ]' in f


# --- a fake cluster of this host (A) and b ---

@pytest.fixture
def host(empty_pve, monkeypatch, tmp_path):
    """Two online hosts, quorate; host commands answered from these files; what changed is recorded."""
    cluster(empty_pve)
    conf = tmp_path / "corosync.conf"
    conf.write_text(COROSYNC)
    monkeypatch.setattr(renaming, "COROSYNC_CONF", str(conf))
    monkeypatch.setattr(nodes, "COROSYNC_CONF", str(conf))
    monkeypatch.setattr(renaming, "STORAGE_CFG", str(tmp_path / "storage.cfg"))
    monkeypatch.setattr(renaming, "quorate", lambda: True)
    monkeypatch.setattr(renaming, "confirm_yes", lambda msg, typed=False: seen.append("yes"))
    monkeypatch.setattr(renaming, "vm_statuses", lambda ids: {i: "running" for i in ids})
    monkeypatch.setattr(guest, "who_on", lambda ids: {i: ["student"] for i in ids})
    monkeypatch.setattr(renaming.time, "sleep", lambda s: None)
    seen, wrote = [], {}
    h = {"seen": seen, "wrote": wrote, "pve": empty_pve, "conf": conf, "hostname": A}

    def run(*argv, data=None, **kw):
        line = " ".join(map(str, argv))
        seen.append(line)
        if "cat > " in line:
            wrote[line.split("cat > ")[1].split(".pvelab-new")[0]] = data
            return ""
        for key, answer in (("cat /etc/hosts", HOSTS), ("cat /etc/hostname", f"{A}\n"),
                            ("cat /etc/corosync/corosync.conf", conf.read_text()),
                            ("pvecm nodes", "    Nodeid Votes Name\n  1 1 ws1 (local)\n  2 1 b\n")):
            if line.endswith(key):
                return answer
        if line.endswith("hostname"):
            return h["hostname"]
        return ""

    def pipe(first, second, logfile=None):
        seen.append("backup " + second[-1])
        os.makedirs(f"{second[-1]}/var/lib/pve-cluster")
        open(f"{second[-1]}/var/lib/pve-cluster/config.db", "w").write("db")
    monkeypatch.setattr(renaming, "run", run)
    monkeypatch.setattr(renaming, "pipe", pipe)
    monkeypatch.setattr(ui, "logged", lambda *cmd: seen.append("logged " + " ".join(map(str, cmd))))
    monkeypatch.setattr(renaming, "node_list", lambda: seen.append("node list"))
    return h


def state():
    with open(renaming.STATE_FILE) as f:
        return json.load(f)


def test_survey_picks_the_way(host):
    vm(host["pve"], A, 201, STUDENT)
    vm(host["pve"], B, 9100, COPY)
    job = renaming.survey(B, "workshop2")
    assert job["mode"] == "rejoin" and job["copies"] == {9100: "debian-13-cloud"} and job["ssh"][0] == "ssh"
    assert renaming.survey(A, "workshop1")["mode"] == "inplace"          # this host: always in place
    vm(host["pve"], B, 301, STUDENT)
    assert renaming.survey(B, "workshop2")["mode"] == "inplace"          # a VM on it


def test_problems(host, monkeypatch, tmp_path):
    assert renaming.problems(renaming.survey(B, "workshop2")) == []
    assert any("not a valid" in p for p in renaming.problems(renaming.survey(B, "bad_name")))
    assert any("already" in p for p in renaming.problems(renaming.survey(B, A)))
    assert renaming.problems(renaming.survey("nothere", "x"))[0].startswith("nothere is not a host")
    (tmp_path / "storage.cfg").write_text("lvmthin: local-lvm\n\tthinpool data\n\tnodes b\n")
    assert any("storage.cfg" in p for p in renaming.problems(renaming.survey(B, "workshop2")))
    host["conf"].write_text(COROSYNC.replace("ring0_addr: 10.0.0.229", "ring0_addr: b"))
    assert any("ring0_addr" in p for p in renaming.problems(renaming.survey(B, "workshop2")))
    monkeypatch.setattr(renaming, "quorate", lambda: False)
    assert any("not quorate" in p for p in renaming.problems(renaming.survey(B, "workshop2")))
    cluster(host["pve"], online_b=False)
    assert any("must be online" in p for p in renaming.problems(renaming.survey(A, "workshop1")))


def test_problems_change_nothing(host):
    with pytest.raises(LabError, match="nothing was changed"):
        renaming.node_rename(B, "bad_name")
    assert not os.path.exists(renaming.STATE_FILE) and "yes" not in host["seen"]


def test_dry_run_shows_the_plan_and_changes_nothing(host, capsys):
    vm(host["pve"], A, 201, STUDENT)
    renaming.node_rename(A, "ws1", dry=True)
    out = capsys.readouterr().out
    assert "in place" in out and "corosync.conf: node" in out and f"reboot {A}" in out
    assert "net30-student01: student" in out and "VMs that restart: net30-student01" in out
    assert "rename-backup-" in out and "dry run - nothing was changed" in out
    assert "yes" not in host["seen"] and not any("cat > " in s or "backup " in s for s in host["seen"])
    assert not os.path.exists(renaming.STATE_FILE) and os.path.exists(f"{host['pve']}/nodes/{A}/qemu-server/201.conf")


def test_this_host_in_place_then_resume_after_its_reboot(host, monkeypatch, capsys):
    vm(host["pve"], A, 201, STUDENT)
    monkeypatch.setattr(renaming, "wait_for", lambda what, test, limit: test() or pytest.fail(what))
    renaming.node_rename(A, "ws1")
    seen, pve = host["seen"], host["pve"]
    assert seen.index("yes") < seen.index(next(s for s in seen if s.startswith("backup ")))
    assert host["wrote"] == {"/etc/hosts": renaming.swap(HOSTS, A, "ws1"), "/etc/hostname": "ws1\n"}
    assert "name: ws1\n" in host["conf"].read_text() and "config_version: 3" in host["conf"].read_text()
    stop = seen.index("logged pvesh --nooutput create /nodes/localhost/stopall")
    assert stop < seen.index("systemctl reboot") and seen[-1] == "systemctl reboot"
    assert os.path.exists(f"{pve}/nodes/ws1/qemu-server/201.conf") and not os.path.exists(f"{pve}/nodes/{A}/qemu-server/201.conf")
    st = state()
    assert st["phase"] == "reboot" and st["running"] == [[201, "net30-student01", True]] and st["local"]
    assert "pvelab command" in capsys.readouterr().out
    # this host is back as ws1
    (pve / ".members").write_text(json.dumps({"nodename": "ws1", "version": 5, "nodelist": {
        "ws1": {"id": 1, "online": 1, "ip": "10.0.0.123"}, "b": {"id": 2, "online": 1, "ip": "10.0.0.229"}}}))
    monkeypatch.setattr(nodes, "this_node", lambda: "ws1")
    monkeypatch.setattr(renaming, "this_node", lambda: "ws1")
    host["hostname"] = "ws1"
    del seen[:]
    renaming.resume()
    assert "logged pvecm updatecerts --force" in seen and "node list" in seen
    assert not os.path.exists(f"{pve}/nodes/{A}") and not os.path.exists(renaming.STATE_FILE)
    assert f"{A} is now ws1" in capsys.readouterr().out


def test_resume_before_the_reboot_says_to_reboot(host, monkeypatch):
    vm(host["pve"], A, 201, STUDENT)
    monkeypatch.setattr(renaming, "wait_for", lambda what, test, limit: None)
    renaming.node_rename(A, "ws1")
    with pytest.raises(LabError, match="reboot it") as e:
        renaming.resume()
    assert "systemctl stop pve-cluster corosync" in str(e.value) and os.path.exists(renaming.STATE_FILE)


def test_empty_host_leaves_and_joins_again(host, monkeypatch):
    vm(host["pve"], B, 9100, COPY)
    monkeypatch.setattr(renaming, "wait_for", lambda what, test, limit: None)
    monkeypatch.setattr(renaming, "rejoin", lambda job: host["seen"].append("rejoin " + job["new"]))
    monkeypatch.setattr(renaming, "done", lambda job: host["seen"].append("rename finished"))
    renaming.node_rename(B, "workshop2")
    seen = host["seen"]
    order = [next(i for i, s in enumerate(seen) if key in s) for key in
             ("backup ", "qm destroy 9100 --purge 1", "logged pvecm delnode b", "pmxcfs -l", "/etc/pve/nodes/b ]",
              "systemctl reboot", "rejoin workshop2", "rename finished")]
    assert order == sorted(order)
    assert host["wrote"]["/etc/hostname"] == "workshop2\n" and "10.0.0.229 workshop2.lab workshop2" in host["wrote"]["/etc/hosts"]
    assert all(s.startswith("ssh") or "ssh " in s for s in seen if "pmxcfs -l" in s)      # on b, not here
    assert state()["phase"] == "rejoin"               # done() was mocked: it removes the file


def test_rejoin_uses_node_adds_join(host, monkeypatch):
    job = renaming.survey(B, "workshop2")
    seen = host["seen"]
    host["hostname"] = "workshop2"

    class R:
        def close(self):
            seen.append("closed")
    monkeypatch.setattr(renaming, "wait_for", lambda what, test, limit: test() or pytest.fail(what))
    monkeypatch.setattr(renaming, "open_remote", lambda job: R())
    monkeypatch.setattr(joining, "local_facts", lambda job: {})
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [])
    monkeypatch.setattr(joining, "join", lambda remote, job: seen.append(f"join {job['name']} {job['addr']}"))
    monkeypatch.setattr(joining, "verify", lambda name: seen.append(f"verify {name}"))
    monkeypatch.setattr(joining, "prepare", lambda name: seen.append(f"prepare {name}"))
    renaming.rejoin(job)
    assert seen[-4:] == ["join workshop2 10.0.0.229", "closed", "verify workshop2", "prepare workshop2"]

    def wrong(remote, job):
        job["step"] = "confirm"
        raise LabError("not accepted 3 times")
    monkeypatch.setattr(joining, "join", wrong)
    with pytest.raises(LabError):
        renaming.rejoin(job)
    assert job["step"] == "join" and "pvelab node add 10.0.0.229" in renaming.failure(job, "x")


def test_resume_after_the_join_goes_straight_to_verify_and_prepare(host, monkeypatch):
    """pvecm add worked, then verify or prepare failed: the next resume must not run node add's checks
    again (NEW is a member now: check_hostname would fail every time)."""
    job = renaming.survey(B, "workshop2")
    (host["pve"] / ".members").write_text(json.dumps({"nodename": A, "version": 6, "nodelist": {
        A: {"id": 1, "online": 1, "ip": "10.0.0.123"}, "workshop2": {"id": 2, "online": 1, "ip": "10.0.0.229"}}}))
    renaming.save(job, "rejoin")
    seen = host["seen"]
    monkeypatch.setattr(renaming, "open_remote", lambda job: pytest.fail("ssh again"))
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: pytest.fail("checks again"))
    monkeypatch.setattr(joining, "verify", lambda name: seen.append(f"verify {name}"))
    monkeypatch.setattr(joining, "prepare", lambda name: seen.append(f"prepare {name}"))
    renaming.resume()
    assert seen.index("verify workshop2") < seen.index("prepare workshop2") < seen.index("node list")
    assert not os.path.exists(renaming.STATE_FILE)


def test_delnode_without_quorum_sets_expected_votes_and_tries_again(host, monkeypatch):
    seen, tries = host["seen"], []

    def logged(*cmd):
        seen.append("logged " + " ".join(map(str, cmd)))
        if cmd[:2] == ("pvecm", "delnode") and not tries:
            tries.append(1)
            raise LabError("cfs lock: no quorum")
        if cmd[:2] == ("pvecm", "delnode"):            # the second time it writes corosync.conf
            cluster(host["pve"], online_b=False)
            (host["pve"] / ".members").write_text(json.dumps({"nodename": A, "version": 7, "nodelist": {
                A: {"id": 1, "online": 1, "ip": "10.0.0.123"}}}))
    monkeypatch.setattr(ui, "logged", logged)
    monkeypatch.setattr(renaming, "wait_for", lambda what, test, limit: test() or renaming.die(what))
    renaming.delnode(B)
    assert [s for s in seen if s.startswith("logged")] == [
        "logged pvecm delnode b", "logged pvecm expected 1", "logged pvecm delnode b"]
    assert "pvecm expected 1" in renaming.STATE["delnode"]


def test_after_reboot_waits_while_the_host_has_no_ip_yet(monkeypatch):
    def no_ip(*a, **k):
        raise LabError("host workshop2 is offline; its VMs can't be reached")
    monkeypatch.setattr(renaming, "on_node", no_ip)
    assert renaming.hostname_of({"old": "b", "ssh": None}, "workshop2") == ""


def test_the_node_settings_move_and_a_dropped_certificate_is_named(host, capsys):
    vm(host["pve"], B, 301, STUDENT)
    old = host["pve"] / "nodes" / B
    (old / "config").write_text("description: lab host\n")
    (old / "host.fw").write_text("[RULES]\nIN ACCEPT -p tcp -dport 22\n")
    (old / "pveproxy-ssl.pem").write_text("cert")
    job = renaming.survey(B, "workshop2")
    renaming.move_configs(job)
    new = host["pve"] / "nodes" / "workshop2"
    assert (new / "config").read_text() == "description: lab host\n" and (new / "host.fw").exists()
    assert (old / "host.fw").exists()                 # until the host has its new name
    (host["pve"] / ".members").write_text(json.dumps({"nodename": A, "version": 6, "nodelist": {
        A: {"id": 1, "online": 1, "ip": "10.0.0.123"}, "workshop2": {"id": 2, "online": 1, "ip": "10.0.0.229"}}}))
    renaming.drop_old_dir(job)
    assert not old.exists() and "pveproxy-ssl.pem" in capsys.readouterr().err


def test_a_failure_says_pvelab_is_blocked_until_the_state_file_goes(host, monkeypatch):
    job = renaming.survey(B, "workshop2")
    job["step"] = "copies"
    renaming.save(job, "changing")
    msg = renaming.failure(job, LabError("qm destroy failed"))
    assert not os.path.exists(renaming.STATE_FILE) and "may run again" in msg and "config.db" not in msg
    job["step"] = "separate"
    renaming.save(job, "changing")
    msg = renaming.failure(job, LabError("x"))
    assert "config.db" in msg and f"rm {renaming.STATE_FILE}" in msg and os.path.exists(renaming.STATE_FILE)
    job["step"] = "hostname"
    assert "out of the cluster" in renaming.failure(job, LabError("x"))


def test_bare_rename_finishes_the_unfinished_one(monkeypatch):
    got = []
    monkeypatch.setattr(node_cmd, "node_rename", lambda old, new, dry=False: got.append((old, new, dry)))
    with pytest.raises(Exception, match="rename OLD NEW"):
        node_cmd.cmd_node_rename(["rename"])                  # nothing unfinished: usage
    open(renaming.STATE_FILE, "w").write("{}")
    node_cmd.cmd_node_rename(["rename"])
    assert got == [(None, None, False)]


def test_open_remote_falls_back_to_asking(monkeypatch):
    made = []

    class Remote:
        def __init__(self, addr, user, opts=()):
            made.append((addr, user, list(opts)))

        def open(self, batch=False):
            return not batch and True
    monkeypatch.setattr(joining, "Remote", Remote)
    monkeypatch.setattr(joining, "ask_user", lambda addr: "sean")
    renaming.open_remote({"old": "b", "addr": "10.0.0.229"})
    assert [m[:2] for m in made] == [("10.0.0.229", "root"), ("10.0.0.229", "sean")]


def test_a_failed_step_says_the_state_and_how_to_restore(host, monkeypatch):
    vm(host["pve"], A, 201, STUDENT)
    real = renaming.run

    def run(*argv, **kw):
        if "cat > /etc/hostname" in " ".join(map(str, argv)):
            raise LabError("disk full")
        return real(*argv, **kw)
    monkeypatch.setattr(renaming, "run", run)
    with pytest.raises(LabError) as e:
        renaming.node_rename(A, "ws1")
    msg = str(e.value)
    assert "step 'hostname' failed: disk full" in msg and "rename-backup-" in msg
    assert "cp " in msg and "config.db" in msg and "reboot" in msg and os.path.exists(renaming.STATE_FILE)
    assert state()["step"] == "hostname"


def test_a_failed_backup_changes_nothing(host, monkeypatch):
    def pipe(first, second, logfile=None):
        raise LabError("tar failed")
    monkeypatch.setattr(renaming, "pipe", pipe)
    with pytest.raises(LabError, match="nothing was changed"):
        renaming.node_rename(B, "workshop2")
    assert not os.path.exists(renaming.STATE_FILE) and not any("delnode" in s for s in host["seen"])


def test_start_vms_starts_what_onboot_did_not(monkeypatch):
    calls = iter([{201: "running", 202: "stopped"}, {202: "stopped"}, {202: "running"}])
    started = []
    monkeypatch.setattr(renaming, "vm_statuses", lambda ids: next(calls))
    monkeypatch.setattr(renaming, "run", lambda *argv, **kw: started.append(argv[-1]))
    monkeypatch.setattr(renaming, "qm_argv", lambda v, *a: ["qm", *a])
    monkeypatch.setattr(renaming, "ONBOOT_SECS", 0)
    monkeypatch.setattr(renaming.time, "sleep", lambda s: None)
    job = {"new": "ws1", "running": [[201, "s1", True], [202, "s2", False]]}
    renaming.start_vms(job)
    assert started == [202] and job["not_started"] == []


# --- wiring ---

@pytest.mark.parametrize("argv, resumed", [(["list"], True), (["node", "rename", A, "ws1"], False),
                                           (["destroy", "all", "--dry-run"], False)])
def test_any_command_finishes_a_pending_rename(monkeypatch, tmp_path, argv, resumed):
    from pvelab import cli
    ran, done = [], []
    open(renaming.STATE_FILE, "w").write("{}")
    monkeypatch.setattr(cli, "pve_check", lambda: None)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "LAB_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CRED", str(tmp_path / "credentials.tsv"))
    monkeypatch.setattr(cli, "ensure_snippet", lambda: None)
    monkeypatch.setattr(cli.fcntl, "flock", lambda f, op: None)
    monkeypatch.setattr(renaming, "resume", lambda: done.append(1))
    name = cli.command(argv[0], argv[1:])
    monkeypatch.setitem(cli.COMMANDS, name, (lambda args, **kw: ran.append(args), cli.COMMANDS[name][1]))
    old = os.umask(0o022)
    try:
        cli.main(argv)
    finally:
        os.umask(old)
    assert bool(done) == resumed and ran


def test_rename_dry_run_reaches_the_command(monkeypatch):
    got = []
    monkeypatch.setattr(node_cmd, "node_rename", lambda old, new, dry=False: got.append((old, new, dry)))
    node_cmd.cmd_node_rename(["rename", "b", "workshop2"], dry=True)
    assert got == [("b", "workshop2", True)]


@pytest.mark.parametrize("words,cword,expect", [
    (["node", "r"], 2, ["rename"]),
    (["node", "rename", ""], 3, ["pve2", "workshops"]),
    (["node", "rename", "pve2", "w2", "--"], 5, ["--dry-run"]),
])
def test_completion(words, cword, expect):
    assert complete.candidates(words, cword, {"nodes": ["pve2", "workshops"]}.get) == expect


def test_help_names_the_order():
    from pvelab.helptext import USAGE, command_help
    assert "node rename OLD NEW" in USAGE and "pve9 -> workshop2" in command_help("node")
    assert ui.LOG is None
