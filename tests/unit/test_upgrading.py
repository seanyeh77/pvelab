"""pvelab node upgrade: the parsers, the apt sources rewrite, the checks and the rolling steps (all mocked:
nothing runs on a host)."""
import pytest

from pvelab import cli, guest, joining, upgrading
from pvelab.commands import complete
from pvelab.commands import node as node_cmd
from pvelab.helptext import command_help
from pvelab.util import LabError

from test_nodes import A, B, cluster

DEBIAN_SOURCES = """Types: deb
URIs: http://deb.debian.org/debian/
Suites: trixie trixie-updates
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security/
Suites: trixie-security
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
PVE_SOURCES = """Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
"""
CEPH_OFF = """Types: deb
URIs: http://download.proxmox.com/debian/ceph-squid
Suites: trixie
Components: no-subscription
Enabled: no
"""
ENTERPRISE_OFF = """Types: deb
URIs: https://enterprise.proxmox.com/debian/pve
Suites: trixie
Components: pve-enterprise
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
Enabled: false
"""


def facts(version="9.2.20", codename="trixie", root_kb=9583344, boot_kb=900000, reboot=False, boot="boot-1"):
    return (f"pve-manager/{version}/0123abcd (running kernel: 6.17.2-1-pve)\ncodename={codename}\n"
            "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
            f"/dev/mapper/pve-root 14173644 9848516 {root_kb} 50% /\n"
            f"/dev/sda2 1046508 100000 {boot_kb} 10% /boot\n"
            + ("reboot-required\n" if reboot else "") + f"boot={boot}\n")


SIM_KERNEL = ("Inst pve-manager [9.2.20] (9.2.21 Proxmox:9.2/trixie [amd64])\n"
              "Inst proxmox-kernel-6.17.4-1-pve-signed (6.17.4-1 Proxmox:9.2/trixie [amd64])\n"
              "Inst proxmox-kernel-helper [9.0.3] (9.0.4 Proxmox:9.2/trixie [all])\n"
              "Conf pve-manager (9.2.21 Proxmox:9.2/trixie [amd64])\n")
SIM_SMALL = "Inst libc-bin [2.41-12] (2.41-13 Debian:13.2/stable [amd64])\n"
SIM_HELPER = "Inst proxmox-kernel-helper [9.0.3] (9.0.4 Proxmox:9.2/trixie [all])\n"     # not a kernel


class Hosts:
    """The hosts, as sh() sees them: per node its version, boot id, pending packages, sources; every
    (node, what) that ran is in .calls."""

    def __init__(self, nodes, sim=None, version="9.2.20", **kw):
        self.state = {n: {"version": version, "boot": f"{n}-1", "sim": (sim or {}).get(n, ""), "codename": "trixie",
                          "files": {"/etc/apt/sources.list.d/debian.sources": DEBIAN_SOURCES,
                                    "/etc/apt/sources.list.d/proxmox.sources": PVE_SOURCES},
                          **kw} for n in nodes}
        self.calls, self.fail, self.checker = [], {}, {}

    def __call__(self, node, script, timeout=60, show=None, data=None):
        s = self.state[node]
        kind = ("reboot" if script.startswith("systemctl reboot") else "audit" if script.startswith("dpkg --audit")
                else "facts" if script.startswith("pveversion") else "simulate" if "-s dist-upgrade" in script
                else "upgrade" if "systemd-run" in script else "follow" if script.startswith("tail ")
                else "update" if script.endswith("apt-get -q update")
                else "checker" if script.startswith("command -v") else "dump" if script.startswith("for f in")
                else "write" if script.startswith("cp -p") else script)
        self.calls.append((node, kind))
        if (node, kind) in self.fail:
            return self.fail[(node, kind)]
        if kind == "facts" or kind == "audit":
            out = facts(s["version"], s["codename"], boot=s["boot"], **s.get("facts", {}))
            return 0, ("==facts\n" + out if kind == "audit" else out)
        if kind == "simulate":
            return 0, s["sim"]
        if kind == "upgrade":                # started in its own unit
            s["version"], s["sim"] = s.get("new", "9.2.21"), ""
            return 0, ""
        if kind == "follow":                 # its log, until the unit ends
            for line in ("Setting up pve-manager (9.2.21) ...\n", "==pvelab-rc=0\n"):
                show(line)
            return 0, "Setting up pve-manager (9.2.21) ...\n==pvelab-rc=0\n"
        if kind == "reboot":
            s["boot"] = f"{node}-2"
            return 255, ""
        if kind == "checker":
            return self.checker.get(node, (0, "= SUMMARY =\nTOTAL: 40\nPASSED: 39\nWARNINGS: 1\nFAILURES: 0\n"))
        if kind == "dump":
            return 0, "".join(f"==pvelab== {p}\n{t}\n" for p, t in s["files"].items())
        if kind == "write":
            path = script.split()[2]
            s["files"][path] = data
            return 0, ""
        return 0, ""

    def ran(self, kind):
        return [n for n, k in self.calls if k == kind]


@pytest.fixture
def two(empty_pve, monkeypatch, tmp_path):
    """Two hosts (this one and b) in a quorate cluster; nobody logged in; LAB_YES=1."""
    cluster(empty_pve)
    conf = tmp_path / "corosync.conf"
    conf.write_text("")
    monkeypatch.setattr(joining, "COROSYNC_CONF", str(conf))
    monkeypatch.setattr(upgrading, "cluster_ok", lambda: True)
    monkeypatch.setattr(upgrading, "logged_in", lambda node: ({}, 0))
    monkeypatch.setattr(upgrading, "drop_master", lambda node: None)
    monkeypatch.setattr(upgrading, "wait_for", lambda what, test, limit: test() or test() or pytest.fail(what))
    monkeypatch.setattr(upgrading, "YES", True)
    upgrading.fetch_release.cache_clear()


def use(monkeypatch, hosts):
    monkeypatch.setattr(upgrading, "sh", hosts)
    return hosts


# --- parsers ---

def test_parsers():
    assert upgrading.parse_pveversion("pve-manager/9.2.20/abc (running kernel: x)") == "9.2.20"
    assert upgrading.parse_pveversion("bash: pveversion: not found") is None and upgrading.major("10.0.3") == 10
    f = upgrading.parse_facts(facts(reboot=True))
    assert f == {"version": "9.2.20", "codename": "trixie", "root_kb": 9583344, "boot_kb": 900000,
                 "reboot": True, "boot_id": "boot-1"}
    assert upgrading.parse_simulation(SIM_KERNEL) == [("pve-manager", "9.2.21"),
                                                      ("proxmox-kernel-6.17.4-1-pve-signed", "6.17.4-1"),
                                                      ("proxmox-kernel-helper", "9.0.4")]
    assert upgrading.parse_checker("\033[1;31mFAILURES: 2\n\033[0m") == 2 and upgrading.parse_checker("x") is None
    assert upgrading.apt_error("W: a\nE: Failed to fetch x\n") == "E: Failed to fetch x"
    assert guest.users_in("student pts/0 2026-10-31 10:00 (1.2.3.4)\nstudent pts/1 x\nroot tty1 x\n") == \
        ["root", "student"]


def test_what_is_upgraded():
    h = {"pkgs": upgrading.parse_simulation(SIM_KERNEL), "kernel": True}
    assert upgrading.what(h) == "3 package(s) to upgrade (a new kernel, pve-manager 9.2.21)"
    assert upgrading.what({"pkgs": [], "kernel": False}) == "up to date"


def test_next_codename(monkeypatch):
    assert upgrading.next_codename("bookworm") == "trixie" and upgrading.next_codename("trixie") == "forky"
    monkeypatch.setattr(upgrading, "fetch_release", lambda uri, suite: "Suite: testing\nCodename: zzz\n")
    assert upgrading.next_codename("duke") == "zzz"


# --- the apt sources ---

def test_rewrite_deb822_sources():
    new = upgrading.rewrite("/x/debian.sources", DEBIAN_SOURCES, "trixie", "forky")
    assert "Suites: forky forky-updates\n" in new and "Suites: forky-security\n" in new
    assert new.replace("forky", "trixie") == DEBIAN_SOURCES          # nothing else changed
    assert upgrading.rewrite("/x/ceph.sources", CEPH_OFF, "trixie", "forky") == CEPH_OFF     # disabled: kept
    assert upgrading.rewrite("/x/pve-enterprise.sources", ENTERPRISE_OFF, "trixie", "forky") == ENTERPRISE_OFF
    for word in ("no", "false", "False", "off", "0", "without", "disable"):
        assert upgrading.entries("/x/a.sources", PVE_SOURCES + f"Enabled: {word}\n") == []
    assert upgrading.entries("/x/a.sources", PVE_SOURCES + "Enabled: yes\n") != []


def test_sources_dump_keeps_each_file_apart():
    # the second file has no last newline: the next marker must not end up on its last line
    files = {"/etc/apt/sources.list": "deb http://deb.debian.org/debian trixie main\n",
             "/etc/apt/sources.list.d/debian.sources": DEBIAN_SOURCES.rstrip("\n"),
             "/etc/apt/sources.list.d/proxmox.sources": PVE_SOURCES}
    dump = "".join(f"==pvelab== {p}\n{t}\n" for p, t in files.items())      # as DUMP_SOURCES prints them
    assert upgrading.parse_sources_dump(dump) == files
    assert "{ echo \"==pvelab== $f\"; cat \"$f\"; echo; }" in upgrading.DUMP_SOURCES


def test_rewrite_one_line_sources():
    text = ("# deb http://old trixie main\n"
            "deb [signed-by=/usr/share/keyrings/k.gpg] http://deb.debian.org/debian trixie main contrib\n"
            "deb http://security.debian.org/debian-security trixie-security main\n"
            "deb http://other bookworm main\n")
    new = upgrading.rewrite("/etc/apt/sources.list", text, "trixie", "forky")
    assert new.splitlines() == ["# deb http://old trixie main",
                                "deb [signed-by=/usr/share/keyrings/k.gpg] http://deb.debian.org/debian forky main contrib",
                                "deb http://security.debian.org/debian-security forky-security main",
                                "deb http://other bookworm main"]
    assert upgrading.entries("/etc/apt/sources.list", new)[0] == (["http://deb.debian.org/debian"], ["forky"],
                                                                   ["main", "contrib"])


RELEASES = {"http://deb.debian.org/debian/": "Suite: stable\nCodename: forky\nComponents: main contrib non-free non-free-firmware\n",
            "http://security.debian.org/debian-security/": "Suite: stable-security\nComponents: updates/main updates/contrib updates/non-free-firmware\n",
            "http://download.proxmox.com/debian/pve": "Suite: forky\nComponents: pve-no-subscription pve-test\n"}


@pytest.mark.parametrize("change, problem", [
    ({}, None),
    ({"http://download.proxmox.com/debian/pve": None}, "http://download.proxmox.com/debian/pve has no forky yet"),
    ({"http://deb.debian.org/debian/": "Suite: testing\nComponents: main contrib non-free-firmware\n"},
     "Debian forky is not released yet (still testing)"),
    ({"http://download.proxmox.com/debian/pve": "Suite: forky\nComponents: pve-test\n"},
     "http://download.proxmox.com/debian/pve forky has no pve-no-subscription yet"),
])
def test_unpublished(monkeypatch, change, problem):
    rel = {**RELEASES, **change}
    monkeypatch.setattr(upgrading, "fetch_release", lambda uri, suite: rel[uri])
    srcs = {p: upgrading.rewrite(p, t, "trixie", "forky") for p, t in
            {"/a/debian.sources": DEBIAN_SOURCES, "/a/proxmox.sources": PVE_SOURCES, "/a/ceph.sources": CEPH_OFF,
             "/a/pve-enterprise.sources": ENTERPRISE_OFF}.items()}     # rel[] has no enterprise: never fetched
    assert upgrading.unpublished(srcs, "forky") == problem


# --- the command ---

def test_minor_upgrade_rolls_other_host_first_and_reboots_this_one_last(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_KERNEL, B: SIM_KERNEL}))
    upgrading.node_upgrade([])
    assert h.ran("upgrade") == [B, A]                            # this host last
    assert h.ran("reboot") == [B, A]                             # a new kernel on both
    out = capsys.readouterr().out
    assert "reboots now" in out and "pvelab node upgrade" in out
    assert "dist-upgrade" in upgrading.START_UPGRADE and "systemd-run" in upgrading.START_UPGRADE
    assert "9.2.20" in out and "9.2.21" in out
    calls = [k for n, k in h.calls if n == B]
    assert calls.index("update") < calls.index("upgrade") < calls.index("reboot")


def test_no_reboot_without_a_kernel(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_SMALL}))
    upgrading.node_upgrade([])
    assert h.ran("upgrade") == [B, A] and h.ran("reboot") == []
    assert "reboots now" not in capsys.readouterr().out


def test_kernel_helper_is_no_kernel(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_HELPER, B: SIM_HELPER}))
    upgrading.node_upgrade([])
    assert h.ran("upgrade") == [B, A] and h.ran("reboot") == []


def test_up_to_date_hosts_are_skipped(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B], sim={B: SIM_SMALL}))
    upgrading.node_upgrade([])
    assert h.ran("upgrade") == [B]
    out = capsys.readouterr().out
    assert "skipped: up to date" in out
    h = use(monkeypatch, Hosts([A, B]))
    upgrading.node_upgrade([])
    assert h.ran("upgrade") == [] and "nothing to do" in capsys.readouterr().out


def test_only_named_hosts(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_SMALL}))
    upgrading.node_upgrade([B])
    assert h.ran("upgrade") == [B] and A not in h.ran("facts")
    with pytest.raises(LabError, match="no host 'nope'"):
        upgrading.node_upgrade(["nope"])


def test_dry_run_changes_nothing(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_KERNEL}))
    upgrading.node_upgrade([], dry=True)
    assert h.ran("upgrade") == [] and h.ran("reboot") == []
    out = capsys.readouterr().out
    assert "dry run - would upgrade" in out and "with two hosts" in out


def test_a_failed_check_changes_nothing(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_SMALL}))
    h.state[B]["facts"] = {"root_kb": 500000}
    with pytest.raises(LabError, match="nothing was changed"):
        upgrading.node_upgrade([])
    assert h.ran("upgrade") == [] and "free on /" in capsys.readouterr().out


def test_offline_host_or_no_quorum_stops(two, monkeypatch, empty_pve):
    use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL}))
    monkeypatch.setattr(upgrading, "cluster_ok", lambda: False)
    with pytest.raises(LabError, match="nothing was changed"):
        upgrading.node_upgrade([A])
    cluster(empty_pve, online_b=False)
    assert upgrading.check_cluster({"all": [A, B]}) == f"offline: {B} (every host must be up)"


def test_failure_stops_and_says_how_to_recover(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_SMALL}))
    h.fail[(B, "upgrade")] = (100, "E: Sub-process /usr/bin/dpkg returned an error code (1)\n")
    with pytest.raises(LabError) as e:
        upgrading.node_upgrade([])
    msg = str(e.value)
    assert f"{B}: step 'upgrade' failed" in msg and "dpkg returned an error" in msg
    assert "dpkg --configure -a; apt-get -f install" in msg and f"{A}: not changed" in msg
    assert h.ran("upgrade") == [B]                                # the other host left alone


def test_failed_upgrade_in_its_unit_stops(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_SMALL}))
    h.fail[(B, "follow")] = (0, "E: Sub-process /usr/bin/dpkg returned an error code (1)\n==pvelab-rc=100\n")
    with pytest.raises(LabError) as e:
        upgrading.node_upgrade([])
    msg = str(e.value)
    assert "dist-upgrade failed (100): E: Sub-process" in msg and "systemctl is-active pvelab-upgrade" in msg
    assert h.ran("audit") == [] and h.ran("follow") == [B]


def test_watching_timeout_never_kills_the_upgrade(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={A: SIM_SMALL, B: SIM_SMALL}))
    h.fail[(B, "follow")] = (124, "Unpacking ...\n")                     # sh() killed only the tail
    with pytest.raises(LabError) as e:
        upgrading.node_upgrade([])
    assert "the upgrade goes on" in str(e.value) and "/var/log/pvelab-upgrade.log" in str(e.value)


def test_half_installed_packages_are_not_fine(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={B: SIM_SMALL}))
    h.fail[(B, "audit")] = (0, "The following packages are only half configured:\n pve-manager\n==facts\n"
                            + facts("9.2.21"))
    with pytest.raises(LabError, match="half-installed"):
        upgrading.node_upgrade([])


def test_students_logged_in_asks(two, monkeypatch):
    h = use(monkeypatch, Hosts([A, B], sim={B: SIM_SMALL}))
    monkeypatch.setattr(upgrading, "logged_in", lambda node: ({"net30-student01": ["student"]}, 0))
    monkeypatch.setattr(upgrading, "YES", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(LabError) as e:
        upgrading.node_upgrade([B])
    assert "LAB_YES=1" in str(e.value) and h.ran("upgrade") == []


def test_logged_in_uses_the_guest_agent(empty_pve, monkeypatch):
    from test_nodes import vm
    cluster(empty_pve)
    vm(empty_pve, B, 301, "name: net30-student01\ntags: lab-student\n#managed-by=lab.sh\n")
    vm(empty_pve, B, 302, "name: net30-student02\ntags: lab-student\n#managed-by=lab.sh\n")
    monkeypatch.setattr(upgrading, "vm_statuses", lambda ids: {i: "running" for i in ids})
    answers = {301: {"out-data": "student pts/0 2026-10-31 10:00 (1.2.3.4)\n"}, 302: None}
    monkeypatch.setattr(guest, "guest_json", lambda *a, **kw: answers[a[1]])
    assert upgrading.logged_in(B) == ({"net30-student01": ["student"]}, 1)
    assert upgrading.logged_in(A) == ({}, 0)


# --- --major ---

def test_major_without_a_checker_says_there_is_no_new_version(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B]))
    h.checker = {n: (127, "pvelab: no pve9to10\n") for n in (A, B)}
    with pytest.raises(LabError, match="nothing was changed"):
        upgrading.node_upgrade([], major_=True)
    assert "PVE 10 is not out yet" in capsys.readouterr().out and h.ran("upgrade") == []


def test_major_with_checker_failures_stops(two, monkeypatch, capsys):
    h = use(monkeypatch, Hosts([A, B]))
    h.checker = {B: (0, "FAILURES: 3\n")}
    with pytest.raises(LabError):
        upgrading.node_upgrade([], major_=True)
    assert "3 FAILURE(S)" in capsys.readouterr().out and h.ran("write") == []


def test_major_moves_the_sources_with_a_backup(two, monkeypatch, capsys):
    monkeypatch.setattr(upgrading, "fetch_release", lambda uri, suite: RELEASES[uri])
    h = use(monkeypatch, Hosts([A, B], new="10.0.1"))
    upgrading.node_upgrade([], major_=True)
    assert h.ran("upgrade") == [B, A] and h.ran("reboot") == [B, A]
    files = h.state[B]["files"]
    assert "Suites: forky forky-updates" in files["/etc/apt/sources.list.d/debian.sources"]
    assert "Suites: forky" in files["/etc/apt/sources.list.d/proxmox.sources"]
    calls = [k for n, k in h.calls if n == B]
    assert calls.index("write") < calls.index("update", calls.index("write")) < calls.index("upgrade")
    out = capsys.readouterr().out
    assert "9.2.20" in out and "10.0.1" in out and "*.pvelab-bak" in out


def test_major_disabled_enterprise_is_neither_fetched_nor_moved(two, monkeypatch, capsys):
    monkeypatch.setattr(upgrading, "fetch_release", lambda uri, suite: RELEASES[uri])    # KeyError if enterprise
    h = use(monkeypatch, Hosts([A, B], new="10.0.1"))
    for n in (A, B):
        h.state[n]["files"]["/etc/apt/sources.list.d/pve-enterprise.sources"] = ENTERPRISE_OFF
    upgrading.node_upgrade([], major_=True)
    assert h.state[B]["files"]["/etc/apt/sources.list.d/pve-enterprise.sources"] == ENTERPRISE_OFF
    assert h.ran("write").count(B) == 2                                  # debian and proxmox only
    out = capsys.readouterr().out
    assert "check with:\n  pvelab node upgrade\n" in out                 # not --major: PVE 11 is not out


def test_major_rerun_skips_hosts_already_on_the_new_version(two, monkeypatch):
    monkeypatch.setattr(upgrading, "fetch_release", lambda uri, suite: RELEASES[uri])
    h = use(monkeypatch, Hosts([A, B], new="10.0.1"))
    h.state[B].update(version="10.0.1", codename="forky")
    upgrading.node_upgrade([], major_=True)
    assert h.ran("upgrade") == [A] and h.ran("checker") == [A]


def test_major_not_on_the_new_version_after_upgrade_fails(two, monkeypatch):
    monkeypatch.setattr(upgrading, "fetch_release", lambda uri, suite: RELEASES[uri])
    h = use(monkeypatch, Hosts([A, B], new="9.2.21"))
    with pytest.raises(LabError, match="not PVE 10"):
        upgrading.node_upgrade([], major_=True)
    assert h.ran("upgrade") == [B]


# --- wiring: arguments, completion, help ---

def test_cmd_node_upgrade_arguments(monkeypatch):
    got = []
    monkeypatch.setattr(node_cmd, "node_upgrade", lambda hosts, major_=False, dry=False, minor=False: got.append((hosts, major_, dry, minor)))
    node_cmd.cmd_node_upgrade(["upgrade", "--major", "pve9"])
    node_cmd.cmd_node_upgrade(["upgrade"], dry=True)
    node_cmd.cmd_node_upgrade(["upgrade", "--minor"])
    assert got == [(["pve9"], True, False, False), ([], False, True, False), ([], False, False, True)]
    with pytest.raises(LabError, match="not both"):
        node_cmd.cmd_node_upgrade(["upgrade", "--major", "--minor"])
    with pytest.raises(LabError, match="unknown option --force"):
        node_cmd.cmd_node_upgrade(["upgrade", "--force"])
    with pytest.raises(LabError, match="--dry-run works with destroy, resize, rollback, node rename, node upgrade"):
        cli.main(["node", "list", "--dry-run"])


def test_completion_and_help():
    data = {"nodes": ["pve9", "workshops"]}.get
    assert complete.candidates(["node", "up"], 2, data) == ["upgrade"]
    assert complete.candidates(["node", "upgrade", ""], 3, data) == ["pve9", "workshops", "--major", "--minor", "--dry-run"]
    assert complete.candidates(["node", "add", ""], 3, data) == []
    h = command_help("node")
    assert "node upgrade [HOST...] [--major|--minor] [--dry-run]" in h and "pvelab-bak" in h and "pve9to10 --full" in h


def test_a_published_major_is_offered_only_without_major_or_minor(monkeypatch, capsys):
    from pvelab import upgrading
    asked = []
    monkeypatch.setattr(upgrading, "run_checks", lambda job: job["hosts"][next(iter(job["hosts"]))].update(
        version="9.2.24", codename="trixie", pkgs=[]) or [])
    monkeypatch.setattr(upgrading, "members", lambda: {"a": {"ip": "10.0.0.1", "online": True}})
    monkeypatch.setattr(upgrading.ui, "start_log", lambda d, n: None)
    monkeypatch.setattr(upgrading, "major_out", lambda job: asked.append(job["major"]) or "PVE 10 (Debian trixie -> forky)")
    upgrading.node_upgrade([], minor=True)
    assert asked == []                                   # --minor: not even looked for
    upgrading.node_upgrade([])                           # no terminal: only said
    assert asked == [False] and "PVE 10 (Debian trixie -> forky) is out" in capsys.readouterr().out
