"""pvelab status: log parsing, attacks vs. students' typos, cluster parsing, and the screen. No host
commands: the reads are faked."""
import json
import os

import pytest

from pvelab import logins
from pvelab.commands import status
from pvelab.helptext import command_help
from pvelab.ui import ANSI
from pvelab.util import LabError

T = 1_761_900_000                               # a time in the window


def ssh(ts, msg, ident="sshd-session"):
    return f"{ts}.000001 workshops {ident}[123]: {msg}"


def fails(ip, n, user="root", t0=T):
    return [ssh(t0 + k, f"Failed password for {user} from {ip} port {4000 + k} ssh2") for k in range(n)]


def test_parse_lines():
    ev = logins.parse([
        ssh(T, "Failed password for root from 77.239.124.41 port 5 ssh2"),
        ssh(T, "Failed password for invalid user admin from 77.239.124.69 port 5 ssh2"),
        ssh(T, "Invalid user oracle from 1.2.3.4 port 22", ident="sshd"),
        ssh(T, "Accepted publickey for root from 10.0.0.229 port 5 ssh2"),
        f"{T}.5 workshops pvedaemon[9]: authentication failure; rhost=::ffff:5.6.7.8 user=root@pam msg=x",
        ssh(T, "Connection closed by 1.2.3.4 port 22 [preauth]"),
        "-- No entries --",
    ])
    assert [(e.kind, e.user, e.ip) for e in ev] == [
        ("fail", "root", "77.239.124.41"), ("fail", "admin", "77.239.124.69"), ("invalid", "oracle", "1.2.3.4"),
        ("accept", "root", "10.0.0.229"), ("web", "root@pam", "5.6.7.8")]


@pytest.mark.parametrize("ip,inside", [("10.0.0.5", True), ("10.10.40.11", True), ("127.0.0.1", True),
                                       ("192.0.2.9", True), ("77.239.124.41", False), ("nonsense", False)])
def test_inside_and_outside(ip, inside):
    assert logins.is_internal(ip, {"192.0.2.9"}) is inside


def test_host_attack_counts_and_rate():
    ev = logins.parse(fails("77.239.124.41", 90) + fails("77.239.124.69", 15, "admin") + fails("10.0.0.5", 2)
                      + fails("77.1.1.1", 5, t0=T - 7200))           # before the window: not counted
    t = logins.tally(ev, T - 1, 3600)
    assert t["fails"] == 107 and t["rate_high"]
    assert t["outside"] == {"77.239.124.41": 90, "77.239.124.69": 15} and t["inside"] == {"10.0.0.5": 2}
    assert t["users"]["root"] == 92 and t["users"]["admin"] == 15 and not t["guessed"] and not t["own"]
    assert not logins.tally(ev[:50], T - 1, 3600)["rate_high"]


def test_student_typos_are_not_an_attack():
    """A known IP (logged in as the student before the window): its failures are 'wrong password'."""
    ev = logins.parse(fails("203.0.113.7", 25, "student")
                      + [ssh(T + 30, "Accepted password for student from 203.0.113.7 port 1 ssh2")])
    t = logins.tally(ev, T - 1, 3600, student="student", known={"203.0.113.7"})
    assert t["own"] == {"203.0.113.7": 25} and not t["outside"] and not t["guessed"] and not t["odd"]


def test_a_student_logging_in_today_after_a_typo():
    ev = logins.parse(fails("203.0.113.7", 2, "student")
                      + [ssh(T + 5, "Accepted password for student from 203.0.113.7 port 1 ssh2")])
    t = logins.tally(ev, T - 1, 3600, student="student")
    assert t["own"] == {"203.0.113.7": 2} and not t["outside"]


def test_login_after_many_failures_is_an_alarm():
    ev = logins.parse(fails("198.51.100.4", 20, "student")
                      + [ssh(T + 60, "Accepted password for student from 198.51.100.4 port 1 ssh2")])
    t = logins.tally(ev, T - 1, 3600, student="student")
    assert t["guessed"] == [("198.51.100.4", 20, "student")] and t["outside"] == {"198.51.100.4": 20}
    assert not t["own"]
    known = logins.tally(ev, T - 1, 3600, student="student", known={"198.51.100.4"})
    assert not known["guessed"]


def test_outside_login_as_root_or_someone_else():
    ev = logins.parse([ssh(T, "Accepted password for root from 198.51.100.4 port 1 ssh2"),
                       ssh(T, "Accepted password for bob from 198.51.100.5 port 1 ssh2"),
                       ssh(T, "Accepted password for root from 10.10.40.1 port 1 ssh2"),
                       ssh(T, "Accepted password for student from 198.51.100.6 port 1 ssh2")])
    t = logins.tally(ev, T - 1, 3600, student="student")
    assert t["odd"] == [("198.51.100.4", "root"), ("198.51.100.5", "bob")]
    assert not logins.tally(ev, T - 1, 3600)["odd"]            # hosts: only the guessed-password alarm


@pytest.mark.parametrize("v,secs", [("1h", 3600), ("30m", 1800), ("2d", 172800), ("90s", 90)])
def test_since(v, secs):
    assert status.parse_since(v) == secs and status.parse_since(status.span(secs)) == secs


@pytest.mark.parametrize("v", ["1", "0h", "h", "1w", "-1h"])
def test_since_rejected(v):
    with pytest.raises(LabError, match="--since"):
        status.parse_since(v)


COROSYNC = """totem { cluster_name: pvelab }
nodelist {
  node {
    name: workshops
    nodeid: 1
    quorum_votes: 1
    ring0_addr: 10.0.0.123
  }
  node {
    name: pve9
    nodeid: 2
    ring0_addr: 10.0.0.229
  }
}
"""
CFGTOOL = """Local node ID 1, transport knet
nodeid: 2 reachable
   LINK: 0 udp (10.0.0.123->10.0.0.229) enabled connected mtu: 1397
nodeid: 3 reachable
   LINK: 0 udp (10.0.0.123->10.0.0.5) enabled mtu: 1397
"""
PVECM = """Cluster information
-------------------
Name:             pvelab
Quorum information
------------------
Quorate:          Yes

Votequorum information
----------------------
Expected votes:   3
Highest expected: 3
Total votes:      2
Quorum:           2
Flags:            Quorate Qdevice
"""


def test_cluster_parsers():
    assert status.parse_corosync_nodes(COROSYNC) == {1: "workshops", 2: "pve9"}
    assert status.parse_links(CFGTOOL.splitlines()) == {2: True, 3: False}
    assert status.parse_cluster(PVECM.splitlines()) == {"quorate": True, "votes": 2, "expected": 3, "qdevice": True}
    assert status.parse_cluster(["Error: Corosync config '/etc/pve/corosync.conf' does not exist"]) is None
    assert status.parse_rtt("10.0.0.229 rtt min/avg/max/mdev = 0.312/0.401/0.510/0.050 ms") == 0.401
    assert status.parse_rtt("10.0.0.229 ") is None


def test_who_and_sections():
    out = ("@now 1761900000\n@date 2026-10-31 10:42\n@who\n"
           "student  pts/0        2026-10-31 10:30 (203.0.113.7)\nroot     tty1         2026-10-31 08:00\n"
           "@ssh\n0 0 10.10.40.11:22 203.0.113.7:5555\n@net\nup\n")
    sec = status.sections(out)
    assert sec["now"] == ["1761900000"] and sec["net"] == ["up"] and len(sec["ssh"]) == 1
    assert status.parse_who(sec["who"], sec["date"][0]) == [("student", "203.0.113.7", 12), ("root", "console", 162)]


def test_who_other_layouts():
    """A logind session without a tty ('sshd pts/0', two words) and the C locale's date ('Oct 31 10:30')."""
    lines = ["student  sshd pts/0   2026-10-31 10:30 (1.2.3.4)", "root     pts/1        Oct 31 10:30 (5.6.7.8)",
             "root     tty1         Dec  31 23:50", "x        pts/2        Okt 31 10:30 (9.9.9.9)"]
    assert status.parse_who(lines, "2026-10-31 10:42") == [("student", "1.2.3.4", 12), ("root", "5.6.7.8", 12),
                                                            ("root", "console", 436972)]
    assert "LC_ALL=C.UTF-8 who" in status.vm_script()


def test_failed_none_counts_once():
    """sshd logs 'Failed none' for an invalid user just before 'Failed password': one attempt, one failure."""
    ev = logins.parse([ssh(T, "Failed none for invalid user admin from 1.2.3.4 port 5 ssh2"),
                       ssh(T, "Failed password for invalid user admin from 1.2.3.4 port 5 ssh2")])
    assert [(e.kind, e.user) for e in ev] == [("fail", "admin")]


def test_journal_reads_only_what_is_new():
    j = status.Journal()
    assert j.since(3600) == "-3600s"
    j.add(fails("1.2.3.4", 3), T + 10, 3600)
    assert j.since(3600) == f"@{T + 10 - status.OVERLAP}"
    j.add(fails("1.2.3.4", 3)[2:] + fails("1.2.3.4", 1, t0=T + 20), T + 30, 3600)     # one line seen again
    assert len(j.events()) == 4
    j.add([], T + 3615, 3600)                                   # the old lines leave the window
    assert [e.ts for e in j.events()] == [pytest.approx(T + 20, abs=0.01)]


def test_scripts_hold_no_secret_and_read_since():
    assert "--since=@123" in status.vm_script("@123") and "@log" not in status.vm_script()
    host = status.host_script("pve9", ["10.0.0.123"], "-3600s")
    assert "ping" in host and "corosync-cfgtool -n" in host and "-t pvedaemon --since=-3600s" in host
    assert "pvecm" not in host and "ping" not in status.host_script("x", [], "-1s")


# --- the whole command, with every read faked ---

def snapshot(monkeypatch, empty_pve, vms=None, rows=None, exposed=(), host_out=None, agent=None):
    """status.Status(...).refresh() on a fake two-host cluster."""
    A = os.uname().nodename.split(".")[0]          # this node
    (empty_pve / ".members").write_text(json.dumps({"nodename": A, "version": 4, "nodelist": {
        A: {"id": 1, "online": 1, "ip": "10.0.0.123"}, "pve9": {"id": 2, "online": 1, "ip": "10.0.0.229"}}}))
    vms = vms or {}
    monkeypatch.setattr(status, "managed", lambda: vms)
    monkeypatch.setattr(status, "cred_rows", lambda: rows or {})
    monkeypatch.setattr(status, "is_exposed", lambda n: bool(exposed))
    monkeypatch.setattr(status, "forwarded_on", lambda nets: set(exposed))
    monkeypatch.setattr(status, "vm_stats", lambda: {i: {"status": c.get("_status", "running")} for i, c in vms.items()})
    monkeypatch.setattr(status, "pvesh_json", lambda path: [{"zone": "pvelab", "peers": "10.0.0.123,10.0.0.229"}])
    monkeypatch.setattr(status, "read_text", lambda path: COROSYNC.replace("workshops", A))
    monkeypatch.setattr(status, "sh_on", lambda node, script, timeout=30: (host_out or (lambda n, s: None))(node, script))
    monkeypatch.setattr(status, "agent_out", lambda vmid, *cmd, **kw: (agent or (lambda v, s: None))(vmid, cmd[-1]))
    return A


def conf(name, net="40", node="pve9", **k):
    return {"name": name, "_node": node, "description": f"managed-by=lab.sh net={net}", **k}


def host_reply(node, script):
    links = "@links\nnodeid: 1 reachable\n LINK: 0 udp enabled connected\nnodeid: 2 reachable\n LINK: 0 enabled connected"
    log = "\n".join(fails("77.239.124.41", 150) + fails("10.0.0.5", 1))
    return (f"@now {T + 100}\n@rtt 10.0.0.229 rtt min/avg/max/mdev = 0.3/0.4/0.5/0.1 ms\n"
            f"@rtt 10.0.0.123 rtt min/avg/max/mdev = 0.3/0.5/0.5/0.1 ms\n{links}\n"
            + ("@pvecm\n" + PVECM if "pvecm" in script else "") + f"@log\n{log}\n")


def vm_reply(vmid, script):
    if vmid == 203:
        return None                                     # agent not answering
    if "Accepted" in script and "@now" not in script:   # the 7-day read
        return ssh(T - 86400, "Accepted password for student from 203.0.113.7 port 1 ssh2") + "\n"
    who = "student  pts/0  2026-10-31 10:30 (203.0.113.7)\n" if vmid == 201 else ""
    log = "\n".join(fails("203.0.113.7", 30, "student") + fails("198.51.100.9", 25)) if "@log" in script else ""
    return (f"@now {T + 100}\n@date 2026-10-31 10:42\n@who\n{who}@ssh\n" + ("x 22\n" if who else "")
            + f"@net\n{'down' if vmid == 202 else 'up'}\n@log\n{log}\n")


def test_status_screen(monkeypatch, empty_pve):
    vms = {201: conf("net40-student01"), 202: conf("net40-student02"), 203: conf("net40-student03"),
           204: conf("net40-student04", _status="stopped"), 205: conf("net40-student05")}
    rows = {i: {"user": "student", "password": "SECRETpw1234", "net": "40", "ip": "-"} for i in vms}
    a = snapshot(monkeypatch, empty_pve, vms, rows, exposed={201}, host_out=host_reply, agent=vm_reply)
    lines = [ANSI.sub("", line) for line in status.render(status.Status("40", 3600).refresh())]
    text = "\n".join(lines)
    assert "SECRETpw" not in text
    assert any(line.split()[:2] == [a, "online"] and "pve9: link up, 0.4 ms" in line for line in lines)
    assert "cluster: quorate, votes 2 of 3 expected, QDevice set up" in text
    assert "not checked (pvelab node add HOST checks UDP 4789)" in text
    assert any("net40-student01" in line and "student from 203.0.113.7 0:12" in line and "pve9" in line for line in lines)
    assert "2 VMs: nobody logged in (no internet: net40-student02)" in text
    assert "stopped (1): net40-student04" in text and "agent not answering (1): net40-student03" in text
    assert f"{a} ssh: 151 failed (151/h)" in text and "outside: 77.239.124.41 150" in text and "inside: 10.0.0.5 1" in text
    assert "net40-student01 ssh (exposed): 55 failed" in text
    assert "wrong password (student): 203.0.113.7 30" in text and "outside: 198.51.100.9 25" in text
    assert "hint: many attempts from outside" in text and "guessed" not in text


def test_vms_of_an_offline_host_are_not_stopped():
    """/cluster/resources says 'unknown' for the VMs of an offline host: not 'stopped'."""
    vm = {"agent": False, "sessions": [], "ssh": 0, "internet": "?"}
    snap = {"net": "40", "multi": True, "hosts": {"a": {"online": True}, "b": {"online": False}},
            "vms": [dict(vm, name="s1", node="a", status="stopped"), dict(vm, name="s2", node="b", status="unknown"),
                    dict(vm, name="s3", node="a", status="unknown")]}
    text = "\n".join(ANSI.sub("", line) for line in status.vm_lines(snap))
    assert "stopped (1): s1" in text and "host offline (1): s2" in text and "status unknown (1): s3" in text


def test_login_counts_of_a_silent_host_are_marked(monkeypatch, empty_pve):
    """A host that answered once and then not: its last numbers stay, marked as not answering."""
    replies = [host_reply]
    a = snapshot(monkeypatch, empty_pve, host_out=lambda n, s: replies[0](n, s))
    st = status.Status(None, 3600)
    text = "\n".join(ANSI.sub("", line) for line in status.login_lines(st.refresh()))
    assert f"{a} ssh: 151 failed" in text and "not answering" not in text
    replies[0] = lambda n, s: None
    text = "\n".join(ANSI.sub("", line) for line in status.login_lines(st.refresh()))
    assert f"{a} ssh (as of " in text and ", not answering): 151 failed" in text


def test_status_one_host_no_cluster(monkeypatch, empty_pve, capsys):
    monkeypatch.setattr(status, "managed", lambda: {})
    monkeypatch.setattr(status, "cred_rows", lambda: {})
    monkeypatch.setattr(status, "vm_stats", lambda: {})
    monkeypatch.setattr(status, "pvesh_json", lambda path: [])
    monkeypatch.setattr(status, "read_text", lambda path: "")
    monkeypatch.setattr(status, "sh_on", lambda node, script, timeout=30: f"@now {T}\n@pvecm\n@log\n")
    status.cmd_status([])
    out = capsys.readouterr().out
    assert "cluster: none (one host)" in out and "no student VMs" in out and "no failed logins" in out
    assert "VXLAN" not in out


def test_status_rejects_unknown_options():
    with pytest.raises(LabError, match="unknown option"):
        status.cmd_status(["--watch"])


def test_frame_fits_the_window():
    lines = [f"line {k} " + "x" * 200 for k in range(50)]
    out = status.frame(lines, 60, 10)
    assert len(out) == 9 and all(len(ANSI.sub("", line)) <= 59 for line in out) and "42 more lines" in out[-1]
    assert status.frame(["a"], 60, 10) == ["a"]


def test_follow_redraws_until_ctrl_c(monkeypatch, capsys):
    """-f without a terminal: each reading once, plain; Ctrl-C ends it quietly."""
    snap = {"time": T, "window": 3600, "net": None, "hosts": {"h": {"online": True, "reached": True, "ip": "1",
            "links": None, "rtt": {}}}, "cluster": None, "names": {}, "zone": None, "multi": False, "vms": [],
            "exposed": [], "logins": {}, "host_ips": set()}

    class Fake:
        n = 0

        def refresh(self):
            self.n += 1
            if self.n > 2:
                raise KeyboardInterrupt
            return snap
    monkeypatch.setattr(status.time, "sleep", lambda s: None)
    status.follow(Fake())
    out = capsys.readouterr().out
    assert out.count("Hosts") == 2 and "\033" not in out


def test_help_and_completion():
    from pvelab import cli
    from pvelab.commands import complete
    assert cli.COMMANDS["status"][1] == "read"                   # no lock: runs during a create
    h = command_help("status")
    assert "--since" in h and "-f" in h and "wrong password (student)" in h
    assert complete.candidates(["status", "--"], 2, lambda k: []) == ["--net", "--since"]
    assert complete.candidates(["status", "--net", ""], 3, lambda k: ["40"]) == ["40", "dhcp"]
