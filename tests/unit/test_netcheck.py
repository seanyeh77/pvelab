"""pvelab net check: a VM's view of a bridge (DHCP, gateway, DNS, package server), tried with a namespace."""
import pytest

from pvelab import netcheck
from pvelab.util import LabError


def test_mirror_of_a_template():
    assert netcheck.mirror_of("ubuntu-2604-cloud") == "archive.ubuntu.com"
    assert netcheck.mirror_of("debian-13-cloud") == "deb.debian.org" and netcheck.mirror_of("docker-lab") == "deb.debian.org"


def test_a_new_mac_is_local_and_unicast():
    mac = netcheck.new_mac()
    assert mac.startswith("02:") and len(mac.split(":")) == 6


def test_problem_is_the_first_failed_step_unless_the_packages_are_reached():
    assert netcheck.problem([("DHCP", False, "no DHCP answer")]) == "DHCP: no DHCP answer"
    assert netcheck.problem([("DHCP", True, "x"), ("gateway", False, "no ping"), ("DNS", True, "y"),
                             ("packages", True, "200")]) is None      # a gateway that ignores ping still routes
    assert netcheck.problem([("DHCP", True, "x"), ("gateway", True, "g"), ("DNS", False, "no name")]) == "DNS: no name"


def test_check_cleans_up_when_dhcp_fails(monkeypatch):
    ran = []

    def run(*cmd, **kw):
        ran.append(cmd)
        return '{"error": "no DHCP answer"}' if "python3" in cmd else ""
    monkeypatch.setattr(netcheck, "run", run)
    assert netcheck.check("vmbr0") == [("DHCP", False, "no DHCP answer")]
    assert ran[-2][:3] == ("ip", "link", "del") and ran[-1][:3] == ("ip", "netns", "del")


def test_net_check_command_prints_each_step(monkeypatch, capsys):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd, "check_bridge", lambda v: "vmbr0")
    monkeypatch.setattr(netcmd.netcheck, "check", lambda br, mirror: [("DHCP", True, "192.168.1.5/24"),
                                                                     ("gateway", True, "192.168.1.1"),
                                                                     ("DNS", True, "deb.debian.org is 1.2.3.4"),
                                                                     ("packages", True, "HTTP/1.1 200 OK")])
    netcmd.cmd_net_check(["check", "vmbr0"])
    out = capsys.readouterr().out
    assert "DHCP" in out and "192.168.1.5/24" in out and "a VM on vmbr0 gets a network" in out


def test_net_check_command_fails_with_the_step(monkeypatch):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd, "check_bridge", lambda v: "vmbr0")
    monkeypatch.setattr(netcmd.netcheck, "check", lambda br, mirror: [("DHCP", False, "no DHCP answer")])
    with pytest.raises(LabError, match="DHCP: no DHCP answer"):
        netcmd.cmd_net_check(["check", "vmbr0"])


def test_create_stops_when_an_external_net_gives_no_network(monkeypatch):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    monkeypatch.setattr(create.nets, "resolve", lambda v: "vmbr0" if v == "vmbr0" else None)
    monkeypatch.setattr(create, "on_openstack", lambda: False)
    monkeypatch.setattr(create.netcheck, "check", lambda br, mirror: [("DHCP", False, "no DHCP answer")])
    with pytest.raises(LabError, match="no DHCP answer.*--no-check"):
        create.cmd_create(["1", "debian13", "--net", "vmbr0"])
    assert got == []
    create.cmd_create(["1", "debian13", "--net", "vmbr0", "--no-check"])
    assert [g[0] for g in got] == ["vmbr0-student01"]
