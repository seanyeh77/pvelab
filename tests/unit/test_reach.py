"""pvelab reach: one VM reaches the VMs of every other net of its SDN zone, kept in its record and in its net's
reach section. No host commands: the section's shell is faked."""
import pathlib
import types

import pytest

from pvelab import config, ifaces, networks, nodes, reaches, registry, ui
from pvelab.commands import complete, reach
from pvelab.helptext import UsageError
from pvelab.util import LabError


def net_block(n, name):
    return (f"# lab.sh net {n} begin (managed by lab)\n        # pvelab name {name}\n"
            f"iface lab{n} inet static\n        address 10.{n}.0.1/24\n# lab.sh net {n} end\n")


def vnets(**zone_of):
    """vnets.cfg with vnet labN in zone_of[labN]."""
    pathlib.Path(config.SDN_VNETS).write_text("".join(f"vnet: {v}\n\tzone {z}\n\n" for v, z in zone_of.items()))


def record(vmid, name, net="30", ip="10.30.0.11", **fields):
    d = pathlib.Path(nodes.NODES_DIR) / nodes.this_node() / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(f"name: {name}\nnet0: virtio=BC:24:11:00:00:{vmid % 100:02d},bridge=lab{net}\n"
                                    f"smbios1: uuid=1111-{vmid}\n")
    registry.update({vmid: {"name": name, "net": net, "ip": ip, "user": "student", "password": "pw", **fields}},
                    {vmid: {"smbios1": f"uuid=1111-{vmid}"}})


@pytest.fixture(autouse=True)
def lab(monkeypatch):
    """Nets 30 prod, 31 nonprod (zone SDC) and 32 other (zone Z2), their gateways here; the shell lines in `ran`."""
    with open(config.IFACES, "w") as f:
        f.write(net_block("30", "prod") + net_block("31", "nonprod") + net_block("32", "other"))
    vnets(lab30="SDC", lab31="SDC", lab32="Z2")
    ran = []
    monkeypatch.setattr(ifaces, "run", lambda *cmd, **k: ran.append(cmd[-1]) or "")
    monkeypatch.setattr(ifaces, "subprocess", types.SimpleNamespace(run=lambda *a, **k: None, DEVNULL=None))
    return ran


def section(n="30"):
    m = ifaces.section_re("reach", n).search(open(config.IFACES).read())
    return m.group(0) if m else ""


def test_rules_let_only_its_connections_out_and_their_answers_back():
    assert reaches.rules("30", "10.30.0.11", "BC:24:11:00:00:01", ["31", "32"]) == [
        "FORWARD -i lab30 -s 10.30.0.11 -m mac --mac-source BC:24:11:00:00:01 -o lab31 -j ACCEPT",
        "FORWARD -i lab30 -s 10.30.0.11 -m mac --mac-source BC:24:11:00:00:01 -o lab32 -j ACCEPT",
        "FORWARD -o lab30 -d 10.30.0.11 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"]
    assert reaches.rules("30", "10.30.0.11", "BC:24:11:00:00:01", []) == []


def test_the_zones_other_nets_here_only():
    assert reaches.zone_of("30") == "SDC" and reaches.zone_of("32") == "Z2"
    assert reaches.zone_nets("SDC", "30") == ["31"]


def test_set_reach_writes_the_rules_first_in_their_chain(lab):
    record(100, "wg", reach=True)
    record(101, "web", ip="10.30.0.12")                       # no reach: no rules
    reaches.set_reach("30")
    s = section()
    assert "-I FORWARD -i lab30 -s 10.30.0.11 -m mac --mac-source bc:24:11:00:00:00 -o lab31 -j ACCEPT" in s
    assert "lab32" not in s and "10.30.0.12" not in s        # another zone's net; another VM
    assert "post-down iptables -w 10 -D FORWARD -i lab30 -s 10.30.0.11 -m mac --mac-source bc:24:11:00:00:00 -o lab31" in s
    assert lab and "-I FORWARD" in lab[-1]


def test_no_reach_no_section_and_no_write(lab):
    before = open(config.IFACES).read()
    reaches.set_reach("30")
    assert open(config.IFACES).read() == before and not lab


def test_the_section_follows_the_vm(lab):
    record(100, "wg", reach=True)
    networks.refresh_net("30")
    assert "-s 10.30.0.11 -m mac" in section()
    registry.update({100: {"ip": "10.30.0.20"}}, {100: {"smbios1": "uuid=1111-100"}})     # a new address
    networks.refresh_net("30")
    assert "-s 10.30.0.20 -m mac" in section() and "10.30.0.11" not in section()
    registry.forget([100])                                                               # destroyed
    networks.refresh_net("30")
    assert section() == ""


def test_a_net_that_comes_or_goes_is_reached_or_not(lab):
    record(100, "wg", reach=True)
    reaches.set_reach("30")
    with open(config.IFACES, "a") as f:                      # a new net of the zone
        f.write(net_block("33", "lab33"))
    vnets(lab30="SDC", lab31="SDC", lab32="Z2", lab33="SDC")
    reaches.refresh_all()
    assert "-o lab31" in section() and "-o lab33" in section()
    ifaces.remove_net_block("33")                            # gone again
    reaches.refresh_all()
    assert "-o lab33" not in section() and "-o lab31" in section()


def test_a_new_net_refreshes_the_reach_sections(monkeypatch):
    called, made = [], []
    monkeypatch.setattr(networks, "make_bridge", lambda n: made.append(n) or open(config.IFACES, "a").write(
        net_block(n, "x")))
    monkeypatch.setattr(reaches, "refresh_all", lambda: called.append("refresh"))
    networks.ensure_bridge("34")
    assert made == ["34"] and called == ["refresh"]
    networks.ensure_bridge("30")                             # there already: no refresh
    assert called == ["refresh"]


# --- pvelab reach ---

def test_reach_on_and_off(lab, capsys):
    monkeypatch_yes()
    record(100, "wg")
    reach.cmd_reach(["wg"])
    out = capsys.readouterr().out
    assert "wg (10.30.0.11) reaches the VMs of every other net of zone SDC with its gateway here: nonprod" in out
    assert "only wg's own connections cross" in out and "wg: reaches the other nets of zone SDC" in out
    assert registry.load()["vms"]["100"]["reach"] is True and "-o lab31" in section()
    reach.cmd_reach(["wg"])
    assert "wg reaches nonprod already (zone SDC); its rules made again" in capsys.readouterr().out
    reach.cmd_reach(["wg", "--off"])
    assert "wg: no longer reaches other nets" in capsys.readouterr().out
    assert "reach" not in registry.load()["vms"]["100"] and section() == ""
    reach.cmd_reach(["wg", "--off"])
    assert "wg does not reach other nets" in capsys.readouterr().out


def monkeypatch_yes():
    ui.YES = True


@pytest.fixture(autouse=True)
def restore_yes():
    yes = ui.YES
    yield
    ui.YES = yes


def test_dry_run_changes_nothing(capsys):
    record(100, "wg")
    reach.cmd_reach(["wg"], dry=True)
    assert "dry run - nothing was changed" in capsys.readouterr().out
    assert "reach" not in registry.load()["vms"]["100"] and section() == ""


def test_a_net_in_no_zone_is_refused():
    with open(config.IFACES, "a") as f:
        f.write(net_block("35", "plain"))
    record(100, "wg", net="35", ip="10.35.0.11")
    with pytest.raises(LabError, match=r"plain is in no SDN zone \(a bridge of this host only\)"):
        reach.cmd_reach(["wg"])


def test_listing(capsys):
    record(100, "wg", reach=True)
    reach.cmd_reach([])
    assert "wg" in (out := capsys.readouterr().out) and "zone SDC" in out and "-> nonprod" in out
    registry.update({100: {"reach": None}}, {100: {"smbios1": "uuid=1111-100"}})
    reach.cmd_reach([])
    assert "no VM reaches other nets" in capsys.readouterr().out


def test_a_record_that_cannot_be_written_puts_the_rules_back(monkeypatch):
    monkeypatch_yes()
    record(100, "wg")
    monkeypatch.setattr(reach.legacy, "write", lambda f: (_ for _ in ()).throw(LabError("registry locked")))
    with pytest.raises(LabError, match="registry locked"):
        reach.cmd_reach(["wg"])
    assert section() == ""


@pytest.mark.parametrize("args", [["--off"], ["wg", "nonprod"], ["wg", "--zone"]])
def test_bad_usage(args):
    with pytest.raises(UsageError):
        reach.cmd_reach(args)


def test_completion():
    assert complete.candidates(["reach", "wg", ""], 3, lambda k: []) == ["--off", "--dry-run"]


def test_dry_run_on_a_vm_that_reaches_already_changes_nothing(lab, capsys):
    record(100, "wg", reach=True)
    reach.cmd_reach(["wg"], dry=True)
    assert "its rules would be made again" in capsys.readouterr().out
    assert section() == "" and not lab


def test_a_vm_without_a_mac_gets_no_rules():
    record(100, "wg", reach=True)
    conf = pathlib.Path(nodes.NODES_DIR) / nodes.this_node() / "qemu-server" / "100.conf"
    conf.write_text("name: wg\nsmbios1: uuid=1111-100\n")       # no network card
    reaches.set_reach("30")
    assert section() == ""


def test_a_failed_refresh_is_said_and_the_change_goes_on(monkeypatch, capsys):
    record(100, "wg", reach=True)
    monkeypatch.setattr(reaches, "set_reach", lambda m: (_ for _ in ()).throw(LabError("iptables failed")))
    reaches.refresh_all()
    assert "prod: the rules of the VMs that reach other nets not made again (iptables failed)" in capsys.readouterr().err
