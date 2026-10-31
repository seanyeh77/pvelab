"""pvelab node add: the read-only checks of a new host, the parsers, and the steps (all mocked: nothing runs)."""
import io
import json
import time

import pytest

from pvelab import joining, nodes, resources, util
from pvelab.commands import node as node_cmd
from pvelab.util import LabError

from test_nodes import A, cluster

ADDR = "10.0.0.229"


class FakeRemote:
    """The new host: shell line (or its start) -> (rc, out), or a function of the line giving that."""

    def __init__(self, answers=None, addr=ADDR, user="root", tty_rc=0):
        self.answers, self.addr, self.user, self.tty_rc = {**GOOD, **(answers or {})}, addr, user, tty_rc
        self.cmds, self.closed, self.opened = [], False, False

    def open(self):
        self.opened = True

    def run(self, cmd, timeout=60):
        self.cmds.append(cmd)
        for k, v in self.answers.items():
            if cmd == k or cmd.startswith(k) or (k.startswith("perl:") and cmd.startswith("perl") and k[5:] in cmd):
                return v(cmd) if callable(v) else v
        return 1, ""

    def tty(self, cmd):
        self.cmds.append(cmd)
        out = getattr(self, "tty_out", [""])
        return self.tty_rc if isinstance(self.tty_rc, tuple) else (self.tty_rc, out.pop(0) if len(out) > 1 else out[0])

    def close(self):
        self.closed = True


GOOD = {
    "hostname": (0, "pve2\n"),
    "getent hosts": (0, f"{ADDR}      pve2.lab pve2\n"),
    "pveversion": (0, "pve-manager/9.2.20/0123abcd (running kernel: 6.17.2-1-pve)\n"),
    "find /etc/pve/nodes": (0, ""),
    "test -e /dev/mapper/pve-data_tdata": (0, ""),
    "test -e /dev/kvm": (0, ""),
    "perl:Timeout": (0, "22\n8006\n"),                                   # TCP back to this host
    "perl:LocalPort": lambda cmd: (0, "".join(f"got {p} tok:{p}\n" for p in joining.UDP_PORTS)),
    "perl:or next": (0, ""),                                             # UDP sender
    "date +%s.%N": lambda cmd: (0, f"{time.time():.6f}\n"),
    "timedatectl": (0, "yes\n"),
}


def local(**kw):
    return {"addr": ADDR, "name": "pve2", "ip": "10.0.0.123", "names": {"workshops"}, "version": "9.2", "cluster": False,
            "quorate": None, "ntp": True, "notes": [], **kw}


@pytest.fixture
def net_ok(monkeypatch):
    """This host's side of the network checks: everything arrives."""
    monkeypatch.setattr(joining, "tcp_ok", lambda ip, port: True)
    monkeypatch.setattr(joining, "udp_listen", lambda ports: ({p: None for p in ports}, []))
    monkeypatch.setattr(joining, "udp_received", lambda socks, token, secs=1.0: set(socks))
    monkeypatch.setattr(joining, "udp_send", lambda *a, **kw: None)
    monkeypatch.setattr(joining, "new_token", lambda: "tok")


def test_checks_pass_on_a_good_host(net_ok, capsys):
    loc = local()
    assert joining.run_checks(FakeRemote(), loc) == [] and loc["notes"] == []
    out = capsys.readouterr().out
    assert out.count("✔") == len(joining.CHECKS) and "✘" not in out


@pytest.mark.parametrize("answers, label, fix", [
    ({"getent hosts": (0, "127.0.1.1  pve2.lab pve2\n")}, "hostname resolves", "/etc/hosts"),
    ({"hostname": (0, "workshops\n")}, "hostname", "hostnamectl"),
    ({"pveversion": (0, "pve-manager/9.1.4/abc (running kernel: 6.14)\n")}, "PVE version", "full-upgrade"),
    ({"find /etc/pve/nodes": (0, "/etc/pve/nodes/pve2/qemu-server/100.conf\n")}, "no guests", "vzdump"),
    ({"test -e /dev/mapper/pve-data_tdata": (1, "")}, "storage local-lvm", "LVM-thin"),
    ({"test -e /dev/kvm": (1, "")}, "/dev/kvm", "nested"),
    ({"perl:Timeout": (0, "22\n")}, "TCP 22, 8006", "security group"),
    ({"perl:LocalPort": (0, "")}, "UDP 4789, 5405-5412", "UDP ports"),
    ({"date +%s.%N": lambda cmd: (0, f"{time.time() + 5:.6f}\n")}, "clocks in sync", "chrony"),
    ({"timedatectl": (0, "no\n")}, "clocks in sync", "chrony"),
])
def test_each_check_fails_with_its_own_fix(net_ok, capsys, answers, label, fix):
    loc = local(name=answers["hostname"][1].strip()) if "hostname" in answers else local()   # join_steps reads it
    failed = joining.run_checks(FakeRemote(answers), loc)
    assert [f[0] for f in failed] == [label], failed
    assert fix in failed[0][2] and "ADDR" not in failed[0][2]
    if label == "hostname":                  # the hostname of this host itself
        assert "workshops" in failed[0][1]
    assert "✘ " + label in capsys.readouterr().out


def test_udp_token_that_never_arrives(net_ok, monkeypatch):
    monkeypatch.setattr(joining, "udp_received", lambda socks, token, secs=1.0: set())
    failed = joining.run_checks(FakeRemote(), local())
    assert [f[0] for f in failed] == ["UDP 4789, 5405-5412"]
    assert failed[0][1] == f"UDP 4789, 5405-5412 blocked from {ADDR} to here"


def test_bound_local_port_is_a_note_not_a_failure(net_ok, monkeypatch):
    monkeypatch.setattr(joining, "udp_listen",
                        lambda ports: ({p: None for p in ports if p not in (4789, 5405)}, [4789, 5405]))
    loc = local()
    assert joining.run_checks(FakeRemote(), loc) == []
    assert any("UDP 5405" in n and "allowed" in n for n in loc["notes"]) and len(loc["notes"]) == 2


def test_quorum_of_this_host(net_ok):
    failed = joining.run_checks(FakeRemote(), local(cluster=True, quorate=False))
    assert [f[0] for f in failed] == ["this host's cluster"]


def test_parsers():
    assert joining.parse_version("pve-manager/9.2.20/0123abcd (running kernel: 6.17)") == "9.2"
    assert joining.parse_version("bash: pveversion: not found") is None
    assert joining.parse_getent("10.0.0.229  pve2.lab pve2\n") == ["10.0.0.229"]
    status = "Cluster information\n-------------------\nName:             pvelab\n\nVotequorum information\n"
    assert joining.parse_quorate(status + "Quorate:          Yes\n") is True
    assert joining.parse_quorate(status + "Quorate:          No\n") is False
    assert joining.parse_quorate("Error: Corosync config '/etc/pve/corosync.conf' does not exist") is None
    assert joining.parse_fingerprint("sha256 Fingerprint=ab:CD:01\n") == "AB:CD:01"
    members = {"nodename": "ws", "version": 5, "cluster": {"name": "pvelab", "version": 2, "nodes": 2, "quorate": 1},
               "nodelist": {"ws": {"id": 1, "online": 1, "ip": "10.0.0.123"},
                            "pve2": {"id": 2, "online": 0, "ip": "10.0.0.229"}}}
    assert nodes.parse_members(json.dumps(members)) == {"ws": {"ip": "10.0.0.123", "online": True, "id": 1},
                                                         "pve2": {"ip": "10.0.0.229", "online": False, "id": 2}}
    assert nodes.parse_members('{"nodename": "ws", "version": 0}') == {} and nodes.parse_members("") == {}


# --- node_add, every step mocked ---

@pytest.fixture
def steps(monkeypatch, tmp_path):
    """Record what node_add does; nothing runs. Returns the list of steps / commands seen."""
    seen = []
    monkeypatch.setattr(joining, "verify", lambda name: seen.append(f"verify {name}"))
    monkeypatch.setattr(joining, "prepare", lambda name: seen.append(f"prepare {name}"))
    monkeypatch.setattr(joining, "local_facts", lambda job: local())
    monkeypatch.setattr(joining, "run", lambda *cmd, **kw: seen.append(" ".join(map(str, cmd))) or "")
    monkeypatch.setattr(joining, "api_fingerprint", lambda: "AB:CD")
    monkeypatch.setattr(joining, "COROSYNC_CONF", str(tmp_path / "corosync.conf"))
    monkeypatch.setattr(joining.sys, "stdin", io.StringIO())          # no terminal
    monkeypatch.setattr(joining, "done", lambda job: seen.append(f"done {job['name']}"))
    return seen


def use_remote(monkeypatch, **kw):
    r = FakeRemote(**kw)
    monkeypatch.setattr(joining, "Remote", lambda addr, user: r)
    return r


def test_already_a_member_skips_to_verify_and_prepare(empty_pve, monkeypatch, steps):
    cluster(empty_pve)
    monkeypatch.setattr(joining, "Remote", lambda *a: pytest.fail("no ssh to a member"))
    joining.node_add(ADDR)
    assert steps == ["verify b", "prepare b", "done b"]


def test_this_host_is_refused(empty_pve, steps):
    cluster(empty_pve)
    with pytest.raises(LabError, match="is this host"):
        joining.node_add("10.0.0.123")
    assert steps == []


def test_failed_checks_change_nothing(monkeypatch, steps):
    r = use_remote(monkeypatch)
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [("/dev/kvm", "no /dev/kvm", "enable it")])
    with pytest.raises(LabError) as e:
        joining.node_add(ADDR)
    assert "nothing was changed" in str(e.value) and "step 'checks'" in str(e.value)
    assert not any("pvecm" in s for s in steps + r.cmds) and r.closed


def test_no_cluster_needs_a_typed_yes_even_with_lab_yes(monkeypatch, steps):
    monkeypatch.setattr(util, "YES", True)
    r = use_remote(monkeypatch)
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [])
    with pytest.raises(LabError, match="typed yes"):
        joining.node_add(ADDR)
    assert not any("pvecm" in s for s in steps + r.cmds) and r.closed


def test_join_failure_names_the_step_and_the_recovery(monkeypatch, steps, tmp_path):
    (tmp_path / "corosync.conf").write_text("totem {}\n")               # in a cluster already
    monkeypatch.setattr(joining, "confirm_yes", lambda msg, typed=False: steps.append("asked"))
    r = use_remote(monkeypatch, tty_rc=255)
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [])
    with pytest.raises(LabError) as e:
        joining.node_add(ADDR)
    msg = str(e.value)
    assert "step 'join'" in msg and "pvecm delnode pve2" in msg and "half-joined" in msg and r.closed
    assert r.cmds[-1].startswith("sh -c ") and f"pvecm add 192.0.2.1 --link0 {ADDR} --fingerprint AB:CD" in r.cmds[-1]
    assert not any(s.startswith("pvecm create") for s in steps)


def test_join_needs_a_typed_yes_even_with_lab_yes(monkeypatch, steps, tmp_path):
    (tmp_path / "corosync.conf").write_text("totem {}\n")               # in a cluster already
    monkeypatch.setattr(util, "YES", True)
    r = use_remote(monkeypatch)
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [])
    with pytest.raises(LabError, match="typed yes"):
        joining.node_add(ADDR)
    assert not any("pvecm add" in c for c in r.cmds) and r.closed


def test_verify_of_a_listed_but_offline_host_gives_the_half_joined_recovery(empty_pve, monkeypatch, steps):
    cluster(empty_pve, online_b=False)                                  # a rerun after a failed pvecm add

    def verify(name):
        raise LabError("b online in a quorate cluster: still not ready after 2 min")
    monkeypatch.setattr(joining, "verify", verify)
    with pytest.raises(LabError) as e:
        joining.node_add(ADDR)
    msg = str(e.value)
    assert "step 'join'" in msg and "pmxcfs -l" in msg and "pvecm delnode b" in msg


def test_join_cmd_uses_sudo_for_a_sudoer():
    r = FakeRemote(user="admin")
    line = joining.join_cmd(r, "10.0.0.123", "AB:CD")
    assert line.startswith("sudo -p ") and "cd / && pvecm add 10.0.0.123" in line
    assert joining.join_cmd(FakeRemote(), "10.0.0.123", "AB:CD").startswith("sh -c ")


def test_remote_argvs_hold_no_password(monkeypatch):
    argvs = []

    class Done:
        returncode, stdout = 0, "pve2\n"
    monkeypatch.setattr(joining.subprocess, "run", lambda argv, **kw: argvs.append(argv) or Done())
    r = joining.Remote(ADDR, "admin")
    r.open()
    r.run("hostname")
    r.tty(joining.join_cmd(r, "10.0.0.123", "AB:CD"))
    r.close()
    assert [a for a in argvs[0] if a.startswith("ControlMaster")] == ["ControlMaster=yes"] and "-fN" in argvs[0]
    assert "BatchMode=yes" in argvs[1] and "-t" in argvs[2] and argvs[3][-3:-1] == ["-O", "exit"]
    flat = " ".join(" ".join(a) for a in argvs)
    assert "sshpass" not in flat and "--password" not in flat and "PasswordAuthentication" not in flat
    assert all(f"admin@{ADDR}" in a for a in argvs)


def test_remote_closed_when_checks_crash(monkeypatch, steps):
    r = use_remote(monkeypatch)

    def boom(remote, loc):
        raise LabError("ssh went away")
    monkeypatch.setattr(joining, "run_checks", boom)
    with pytest.raises(LabError, match="ssh went away"):
        joining.node_add(ADDR)
    assert r.closed


def test_node_add_wants_an_ipv4(steps):
    with pytest.raises(LabError, match="IPv4"):
        joining.node_add("pve2.example")


def test_node_list_without_cluster_has_one_row(monkeypatch, capsys):
    monkeypatch.setattr(node_cmd, "inventory", lambda: {})
    monkeypatch.setattr(node_cmd, "templates_by_node", lambda inv: {"debian-13-cloud": {A: "current"}})
    monkeypatch.setattr(node_cmd, "node_mem", lambda: {})
    monkeypatch.setattr(resources, "host_mem_mb", lambda node=None: (8192, 4096))
    monkeypatch.setattr(node_cmd, "storage_status", lambda node: {"total": 200 * 2**30, "used": 100 * 2**30,
                                                                   "avail": 100 * 2**30})
    node_cmd.cmd_node_list([])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("HOST") and "DISK FREE (local-lvm)" in lines[0] and len(lines) == 2
    assert lines[1].split()[:7] == [A, "192.0.2.1", "online", "4.0", "GB", "100", "GB"] and "debian-13-cloud" in lines[1]


def test_node_bad_usage():
    from pvelab.helptext import UsageError
    with pytest.raises(UsageError):
        node_cmd.cmd_node_list(["remove", "pve2"])


def test_ranges_and_blocked():
    assert joining.ranges([5407, 4789, 5405, 5406]) == "4789, 5405-5407"
    assert joining.blocked("UDP", [5405, 5406], [5405, 5406, 4789], ADDR) == \
        f"UDP 5405-5406 blocked both ways; UDP 4789 blocked from here to {ADDR}"
    assert joining.blocked("TCP", [], [], ADDR) == ""


def test_passed_checks_say_what_they_found(net_ok, capsys):
    joining.run_checks(FakeRemote(), local())
    out = capsys.readouterr().out
    assert "UDP 4789 (VXLAN), 5405-5412 (corosync) both ways" in out and "(the same as this host)" in out


def test_ssh_user_defaults_to_the_sudo_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "sean")
    monkeypatch.setattr(joining.sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert joining.ask_user(ADDR) == "sean"


def test_wrong_root_password_is_asked_again_then_gives_up(empty_pve, monkeypatch, steps):
    r = use_remote(monkeypatch)
    r.tty_rc, r.tty_out = 255, ["401 authentication failure\n"] * 3
    monkeypatch.setattr(joining, "confirm_yes", lambda msg, typed=False: None)
    monkeypatch.setattr(joining, "create_cluster", lambda job: None)
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [])
    with pytest.raises(LabError, match="not accepted 3 times") as e:
        joining.node_add(ADDR)
    assert len([c for c in r.cmds if "pvecm add" in c]) == 3
    assert "nothing was changed" in str(e.value)        # the confirm step's state: not changed there


def test_wrong_root_password_then_right_one_joins(empty_pve, monkeypatch, steps):
    r = use_remote(monkeypatch)
    answers = iter([(255, "401 authentication failure\n"), (0, "successfully added node\n")])
    r.tty = lambda cmd: r.cmds.append(cmd) or next(answers)
    monkeypatch.setattr(joining, "confirm_yes", lambda msg, typed=False: None)
    monkeypatch.setattr(joining, "create_cluster", lambda job: None)
    monkeypatch.setattr(joining, "run_checks", lambda remote, loc: [])
    joining.node_add(ADDR)
    assert len([c for c in r.cmds if "pvecm add" in c]) == 2 and steps[-3:] == ["verify pve2", "prepare pve2", "done pve2"]


STATUS = {"cpuinfo": {"model": "Xeon", "sockets": 1, "cores": 4, "cpus": 4, "mhz": "2400.000", "flags": "fpu vmx sse"},
          "memory": {"total": 16 * 2**30, "available": 12 * 2**30}, "swap": {"total": 4 * 2**30},
          "rootfs": {"total": 14 * 2**30, "avail": 7 * 2**30}, "pveversion": "pve-manager/9.2.24/abc",
          "current-kernel": {"release": "7.0.2-6-pve"}, "boot-info": {"mode": "efi"}, "uptime": 90061,
          "loadavg": ["0.10", "0.20", "0.30"]}


def fake_node_list(monkeypatch):
    monkeypatch.setattr(node_cmd, "inventory", lambda: {})
    monkeypatch.setattr(node_cmd, "templates_by_node", lambda inv: {"debian-13-cloud": {A: "current"}})
    monkeypatch.setattr(node_cmd, "node_mem", lambda: {})
    monkeypatch.setattr(resources, "host_mem_mb", lambda node=None: (8192, 4096))
    monkeypatch.setattr(node_cmd, "pvesh_json", lambda path, *a: STATUS)
    monkeypatch.setattr(node_cmd, "storage_status", lambda node: {"total": 100 * 2**30, "used": 40 * 2**30,
                                                                  "avail": 60 * 2**30})


def test_node_list_verbose_shows_the_hardware(monkeypatch, capsys):
    fake_node_list(monkeypatch)
    node_cmd.cmd_node_list(["list", "-v"])
    out = capsys.readouterr().out
    assert "Xeon, 1 socket(s), 4 CPUs, 2400 MHz, VT-x" in out and "16.0 GB, 12.0 GB available" in out
    assert "local-lvm 100.0 GB, 40% used" in out and "PVE 9.2.24, kernel 7.0.2-6-pve, efi boot, up 1d 1h" in out


def test_node_list_json(monkeypatch, capsys):
    fake_node_list(monkeypatch)
    node_cmd.cmd_node_list(["-j"])
    (h,) = json.loads(capsys.readouterr().out)
    assert h["host"] == A and h["online"] and h["cpu"]["threads"] == 4 and h["cpu"]["virtualization"] == "VT-x"
    assert h["storage"]["used_pct"] == 40 and h["pve"] == "9.2.24" and h["load"] == [0.1, 0.2, 0.3]
    assert h["templates"] == ["debian-13-cloud"] and h["disk_free_gb"] == 60     # the details' storage status
