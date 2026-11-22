"""pvelab adopt: a VM made by hand (PVE's web UI) on one of pvelab's nets becomes pvelab's; pvelab list names
such VMs. No host commands: qm and the guest agent are faked."""
import json
import os
import pathlib

import pytest

from pvelab import adopting, config, credentials, inventory, isolation, networks, nodes, owners, registry, ui
from pvelab.commands import handout, listing, owner
from pvelab.helptext import UsageError
from pvelab.util import LabError

MAC = "BC:24:11:AA:BB:CC"


def net_block(n, addr, name=None):
    return (f"# lab.sh net {n} begin (managed by lab)\n" + (f"        # pvelab name {name}\n" if name else "")
            + f"iface vmbr{n} inet static\n        address {addr}\n# lab.sh net {n} end\n")


@pytest.fixture(autouse=True)
def lab_nets(monkeypatch):
    """Net 30 prod (10.30.0.0/24, its gateway here) and net 31 other."""
    with open(config.IFACES, "w") as f:
        f.write(net_block("30", "10.30.0.1/24", "prod") + net_block("31", "10.31.0.1/24", "other"))


def vm(vmid, text, node=None):
    """A VM config on this node (or node), as PVE writes it."""
    d = pathlib.Path(nodes.NODES_DIR) / (node or nodes.this_node()) / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(text)


def hand_made(vmid=100, name="wg", bridge="vmbr30", mac=MAC, extra="", node=None, firewall=0):
    vm(vmid, f"name: {name}\nnet0: virtio={mac},bridge={bridge},firewall={firewall}\n"
             f"smbios1: uuid=0000-{vmid}\n" + extra, node)


def record(vmid, name, net="30", ip="10.30.0.11", **fields):
    """A VM pvelab made (with its record)."""
    vm(vmid, f"name: {name}\nnet0: virtio=BC:24:11:00:00:{vmid % 100:02d},bridge=vmbr{net}\nsmbios1: uuid=1111-{vmid}\n")
    registry.update({vmid: {"name": name, "net": net, "ip": ip, "user": "student", "password": "pw", **fields}},
                    {vmid: {"smbios1": f"uuid=1111-{vmid}"}})


# --- Task 1: finding them; list says so ---

def test_nics_reads_bridge_and_mac():
    conf = {"net1": "e1000=BC:24:11:00:00:01,bridge=vmbr0", "net0": f"virtio={MAC},bridge=vmbr30,firewall=1",
            "name": "x"}
    assert adopting.nics(conf) == [("net0", "vmbr30", "bc:24:11:aa:bb:cc"), ("net1", "vmbr0", "bc:24:11:00:00:01")]


def test_strays_are_hand_made_vms_on_pvelab_nets():
    hand_made(100)
    hand_made(101, "out", "vmbr0")
    hand_made(102, "dhcpvm", "vmbr1")
    hand_made(103, "tpl", extra="template: 1\n")
    record(104, "prod-alice")
    vm(105, "#managed-by=lab.sh net=30\nname: old\ntags: pvelab\nnet0: virtio=BC:24:11:00:00:05,bridge=vmbr30\n")
    vm(106, "name: two\nnet0: virtio=BC:24:11:00:00:06,bridge=vmbr0\nnet1: virtio=BC:24:11:00:00:07,bridge=vmbr30\n")
    found = adopting.strays(inventory.inventory())
    assert [(s.vmid, s.name, s.net, s.nic) for s in found] == [(100, "wg", "30", "net0"), (106, "two", "30", "net1")]


def test_stray_line_texts():
    hand_made(100)
    vm(106, "name: two\nnet0: virtio=BC:24:11:00:00:06,bridge=vmbr0\nnet1: virtio=BC:24:11:00:00:07,bridge=vmbr30\n")
    inv = inventory.inventory()
    wg, two = adopting.strays(inv)
    assert adopting.stray_line(wg, set()) == ("wg (100) on prod: not made by pvelab, so pvelab leaves it alone; "
                                                   "pvelab adopt wg --shared (or --owner NAME) hands it over")
    assert adopting.stray_line(wg, {"30"}) == ("wg (100) on prod: not made by pvelab, so pvelab leaves it alone; "
                                                    "pvelab adopt wg --owner NAME hands it over")
    assert adopting.stray_line(two, set()) == ("two (106) on prod (net1): not made by pvelab, so pvelab leaves it "
                                                    "alone; pvelab adopt takes a VM whose net0 is on its net")
    hand_made(107, "wg")                             # a name used twice: the VMID
    inv = inventory.inventory()
    assert "pvelab adopt 100 --shared" in adopting.stray_line(adopting.strays(inv)[0], set())


def fake_list(monkeypatch):
    monkeypatch.setattr(listing, "exposure", lambda ns: (set(), set()))
    monkeypatch.setattr(listing, "live", lambda: ({}, {}))
    monkeypatch.setattr(listing, "storage", lambda node=None: (50, 100))
    monkeypatch.setattr(listing, "blocks_load", lambda: {})
    monkeypatch.setattr(listing, "vm_ip", lambda i, rows, up=True: "10.30.0.11")
    monkeypatch.setattr(listing.netinfo, "overlaps", lambda n: [])
    monkeypatch.setattr(listing, "is_isolated", lambda n: True)
    monkeypatch.setattr(listing, "host_mem_mb", lambda: (65536, 32768))
    monkeypatch.setattr(listing, "reasons", lambda down: {i: ("stopped", None) for i in down})
    return listing


def test_list_prints_one_line_per_stray(monkeypatch, capsys):
    fake_list(monkeypatch)
    hand_made(100)
    listing.cmd_list([])
    out = capsys.readouterr().out
    assert "(no pvelab VMs)\n\nwg (100) on prod: not made by pvelab" in out
    listing.cmd_list(["--ssh"])
    assert "wg (100)" not in capsys.readouterr().out
    listing.cmd_list(["--net", "other"])
    assert "wg (100)" not in capsys.readouterr().out
    listing.cmd_list(["--net", "prod"])
    assert "wg (100) on prod" in capsys.readouterr().out


def test_list_exposed_prints_no_stray_line(monkeypatch, capsys):
    fake_list(monkeypatch)
    monkeypatch.setattr(listing, "exposure", lambda ns: ({"30"}, set()))
    monkeypatch.setattr(listing, "public_host", lambda: "203.0.113.5")
    hand_made(100)
    record(201, "prod-alice")
    listing.cmd_list(["--exposed"])
    out = capsys.readouterr().out
    assert "prod-alice" in out and "wg (100)" not in out


def test_list_leaves_out_shared_when_the_net_has_one(monkeypatch, capsys):
    fake_list(monkeypatch)
    hand_made(100)
    record(210, "prod-shared", role="shared")
    listing.cmd_list([])
    assert "pvelab adopt wg --owner NAME hands it over" in capsys.readouterr().out


# --- Task 2: the checks ---

def conf_of(vmid=100):
    return inventory.read_conf(vmid)


def test_fixed_address_reads_ipconfig0():
    assert adopting.fixed_address({"ipconfig0": "ip=10.30.0.20/24,gw=10.30.0.1"}) == ("10.30.0.20", 24, "10.30.0.1")
    assert adopting.fixed_address({"ipconfig0": "gw=10.30.0.1,ip=10.30.0.20/24"}) == ("10.30.0.20", 24, "10.30.0.1")
    assert adopting.fixed_address({"ipconfig0": "ip=10.30.0.20/24"}) == ("10.30.0.20", 24, None)
    assert adopting.fixed_address({"ipconfig0": "ip=dhcp"}) is None
    assert adopting.fixed_address({"ipconfig0": "ip6=auto"}) is None
    assert adopting.fixed_address({}) is None


def test_has_cloudinit():
    assert adopting.has_cloudinit({"ide2": "local-lvm:vm-100-cloudinit,media=cdrom"})
    assert not adopting.has_cloudinit({"ide2": "none,media=cdrom", "scsi0": "local-lvm:vm-100-disk-0,size=8G"})


def agent(monkeypatch, answer):
    """The guest agent of every VM answers network-get-interfaces with answer (None: no answer)."""
    asked = []
    monkeypatch.setattr(adopting, "guest_json", lambda *cmd, **k: asked.append(cmd) or answer)
    return asked


def iface(mac, *ips, name="eth0"):
    return {"name": name, "hardware-address": mac,
            "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": a, "prefix": 24} for a in ips]
            + [{"ip-address-type": "ipv6", "ip-address": "fe80::1", "prefix": 64}]}


def test_guest_addresses_reads_the_card_by_its_mac(monkeypatch):
    hand_made()
    hand_made(101, "web", mac="BC:24:11:00:00:01")
    asked = agent(monkeypatch, [iface("00:00:00:00:00:00", "127.0.0.1", name="lo"),
                                iface("bc:24:11:aa:bb:cc", "10.30.0.20", "10.30.0.21")])
    assert adopting.guest_addresses(conf_of()) == ["10.30.0.20", "10.30.0.21"]
    assert asked == [("cmd", 100, "network-get-interfaces")]
    assert adopting.guest_addresses(conf_of(101)) is None      # not reported
    agent(monkeypatch, None)
    assert adopting.guest_addresses(conf_of()) is None


CI = "ide2: local-lvm:vm-100-cloudinit,media=cdrom\n"     # a cloud-init drive: ipconfig0 and ciuser are the guest's


def pick(ip=None, rows=None):
    """pick_ip for VM 100 on prod, its config and every other read now."""
    inv = inventory.inventory()
    return adopting.pick_ip(inv[100], "30", ip, rows or {}, inv)


def test_pick_ip_takes_ipconfig0_in_range():
    hand_made(extra=CI + "ipconfig0: ip=10.30.0.20/24,gw=10.30.0.1\n")
    assert pick() == ("10.30.0.20", "ipconfig0")
    assert pick("10.30.0.20") == ("10.30.0.20", "ipconfig0")
    with pytest.raises(LabError, match=r"^--ip 10.30.0.21, but wg's ipconfig0 gives 10.30.0.20: leave --ip out"):
        pick("10.30.0.21")


def test_pick_ip_refuses_outside_range_and_other_gateway():
    hand_made(extra=CI + "ipconfig0: ip=192.168.1.20/24,gw=192.168.1.1\n")
    with pytest.raises(LabError) as e:
        pick()
    assert str(e.value) == ("wg's ipconfig0 gives 192.168.1.20/24, outside prod (10.30.0.0/24): give it an address of "
                            "prod (qm set 100 --ipconfig0 ip=ADDR/24,gw=10.30.0.1; cloud-init sets it at the next "
                            "boot), then run pvelab adopt again")
    hand_made(extra=CI + "ipconfig0: ip=10.30.0.20/24,gw=10.30.0.254\n")
    with pytest.raises(LabError) as e:
        pick()
    assert str(e.value) == ("wg's ipconfig0 has the gateway 10.30.0.254, prod's is 10.30.0.1: qm set 100 --ipconfig0 "
                            "ip=10.30.0.20/24,gw=10.30.0.1 (cloud-init sets it at the next boot), then run pvelab "
                            "adopt again")
    hand_made(extra=CI + "ipconfig0: ip=10.30.0.20/24\n")
    with pytest.raises(LabError, match="has the gateway none, prod's is 10.30.0.1: qm set 100 --ipconfig0 "
                                       "ip=10.30.0.20/24,gw=10.30.0.1"):
        pick()
    hand_made(extra=CI + "ipconfig0: ip=10.30.0.20/16,gw=10.30.0.1\n")
    with pytest.raises(LabError, match="qm set 100 --ipconfig0 ip=10.30.0.20/24,gw=10.30.0.1"):
        pick()


def test_pick_ip_needs_ip_without_a_fixed_address(monkeypatch):
    hand_made(extra="ipconfig0: ip=dhcp\n")
    agent(monkeypatch, None)
    with pytest.raises(LabError) as e:
        pick()
    assert str(e.value) == ("wg has no fixed address in its config (ipconfig0: ip=dhcp): pvelab needs the address it "
                            "has on prod - give it with --ip ADDR (recorded only; nothing in the VM changes)")
    agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.20", "172.17.0.1")])
    with pytest.raises(LabError, match=r"--ip ADDR \(recorded only; nothing in the VM changes\); its guest agent "
                                       r"reports 10.30.0.20$"):
        pick()
    assert pick("10.30.0.20") == ("10.30.0.20", "--ip")
    hand_made()
    agent(monkeypatch, None)
    with pytest.raises(LabError, match=r"\(ipconfig0: none\)"):
        pick()
    with pytest.raises(UsageError, match=r"^--ip 10.31.0.5: an address of prod \(10.30.0.0/24\)$"):
        pick("10.31.0.5")
    with pytest.raises(UsageError, match=r"^--ip x: an address of prod"):
        pick("x")


def test_pick_ip_refuses_gateway_and_clashes():
    hand_made()
    with pytest.raises(LabError, match=r"^10.30.0.1 is prod's gateway: "):
        pick("10.30.0.1")
    with pytest.raises(LabError, match=r"^10.30.0.255 is no host address of prod \(10.30.0.0/24\)"):
        pick("10.30.0.255")
    rows = {201: {"name": "prod-alice", "net": "30", "ip": "10.30.0.11"}, 301: {"name": "x", "net": "31", "ip": "10.30.0.12"}}
    with pytest.raises(LabError) as e:
        pick("10.30.0.11", rows)
    assert str(e.value) == ("10.30.0.11 is prod-alice's address already: two VMs can't share it - change wg's "
                            "address first")
    assert pick("10.30.0.12", rows) == ("10.30.0.12", "--ip")   # another net's
    hand_made(101, "web", extra="ipconfig0: ip=10.30.0.40/24,gw=10.30.0.1\n")
    hand_made(102, "far", bridge="vmbr31", extra="ipconfig0: ip=10.30.0.41/24,gw=10.30.0.1\n")
    with pytest.raises(LabError, match="^10.30.0.40 is web's address already"):
        pick("10.30.0.40")
    assert pick("10.30.0.41") == ("10.30.0.41", "--ip")


def test_check_agent_refuses_a_different_address(monkeypatch):
    hand_made()
    agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.21")])
    with pytest.raises(LabError) as e:
        adopting.check_agent(conf_of(), "30", "10.30.0.20", "ipconfig0", lambda: "running")
    assert str(e.value) == REFUSALS["agent disagrees"][2]          # ipconfig0's: change that, or the guest's
    with pytest.raises(LabError) as e:
        adopting.check_agent(conf_of(), "30", "10.30.0.20", "--ip", lambda: "running")
    assert str(e.value) == ("wg's guest agent reports 10.30.0.21 on net0, not 10.30.0.20: pvelab's rules would follow "
                            "an address it does not have. Give the address it has with --ip, then run pvelab adopt "
                            "again")
    assert adopting.check_agent(conf_of(), "30", "10.30.0.21", "--ip", lambda: "running") is None


def test_check_agent_warns_only_for_ip_given(monkeypatch):
    hand_made()
    agent(monkeypatch, None)
    states = []

    def status(state):
        return lambda: states.append(state) or state
    assert adopting.check_agent(conf_of(), "30", "10.30.0.20", "--ip", status("stopped")) == (
        "10.30.0.20 not checked (wg is not running): pvelab's rules follow the address recorded")
    assert adopting.check_agent(conf_of(), "30", "10.30.0.20", "--ip", status("running")) == (
        "10.30.0.20 not checked (its guest agent did not answer): pvelab's rules follow the address recorded")
    assert adopting.check_agent(conf_of(), "30", "10.30.0.20", "ipconfig0", status("stopped")) is None
    assert states == ["stopped", "running"]              # its state asked only for --ip


def test_shared_needs_the_fixed_mac_on_an_sdn_net(monkeypatch):
    monkeypatch.setattr(adopting.config, "is_sdn", lambda n: True)
    hand_made(bridge="lab30")
    inv = inventory.inventory()
    why = adopting.shared_problem(inv[100], "30", inv)
    assert why == ("wg's net0 has the MAC bc:24:11:aa:bb:cc; prod's shared VM needs the fixed MAC 02:00:0a:0a:1e:02 "
                   "(VMs on other hosts reach it by that). Shut it down, then: qm set 100 --net0 "
                   "virtio=02:00:0a:0a:1e:02,bridge=lab30,firewall=0; start it and run pvelab adopt again (a guest "
                   "whose network config names the old MAC needs that changed too)")
    hand_made(bridge="lab30", mac="02:00:0A:0A:1E:02")
    inv = inventory.inventory()
    assert adopting.shared_problem(inv[100], "30", inv) is None


def test_shared_mac_not_needed_on_a_bridge_net():
    hand_made()
    inv = inventory.inventory()
    assert adopting.shared_problem(inv[100], "30", inv) is None


def test_shared_refused_when_the_net_has_one():
    hand_made()
    record(210, "box", role="shared")
    inv = inventory.inventory()
    assert adopting.shared_problem(inv[100], "30", inv).startswith(
        "box is prod's shared VM already (one per net: they share a fixed MAC)")


def test_owner_key_drops_the_net_name_and_checks_it():
    hand_made()
    assert adopting.owner_key("prod-alice", "30", conf_of()) == "alice"
    assert adopting.owner_key("alice", "30", conf_of()) == "alice"
    with pytest.raises(UsageError, match="^--owner shared: 'shared' marks"):
        adopting.owner_key("shared", "30", conf_of())
    with pytest.raises(UsageError, match="^--owner other-x: other is another network"):
        adopting.owner_key("other-x", "30", conf_of())
    with pytest.raises(UsageError, match="^--owner: 'Alice' is not a host name"):
        adopting.owner_key("Alice", "30", conf_of())
    hand_made(name="prod-shared")
    with pytest.raises(LabError) as e:
        adopting.owner_key("alice", "30", conf_of())
    assert str(e.value) == ("prod-shared is named as prod's shared VM: --shared, or give it another name first "
                            "(qm set 100 --name NEW)")


def test_login_defaults_to_ciuser_and_refuses_another():
    assert adopting.login({"name": "wg", "ciuser": "debian", "ide2": CI}, None) == "debian"
    assert adopting.login({"name": "wg", "ciuser": "debian", "ide2": CI}, "debian") == "debian"
    with pytest.raises(LabError) as e:
        adopting.login({"name": "wg", "ciuser": "debian", "ide2": CI}, "root")
    assert str(e.value) == ("wg's cloud-init user is debian: pvelab logs in as that (passwd, handout) - leave --user "
                            "out or give --user debian")


def test_login_needs_user_without_ciuser():
    assert adopting.login({"name": "wg"}, "admin") == "admin"
    with pytest.raises(UsageError, match="^--user: 'Admin' is not a login name"):
        adopting.login({"name": "wg"}, "Admin")
    with pytest.raises(LabError) as e:
        adopting.login({"name": "wg"}, None)
    assert str(e.value) == "wg has no cloud-init user (ciuser): --user USER names the login pvelab hands out"


def test_without_a_cloudinit_drive_ciuser_is_only_a_default():
    plain = {"name": "wg", "ciuser": "debian"}       # no cloud-init drive: ciuser is not the guest's truth
    assert adopting.login(plain, None) == "debian"
    assert adopting.login(plain, "admin") == "admin"
    with pytest.raises(LabError, match="^wg has no cloud-init user"):
        adopting.login({"name": "wg"}, None)
    assert inventory.login_of(100, {"user": "admin"}, plain) == "admin"       # pvelab logs in as recorded
    assert inventory.login_of(100, {"user": "admin"}, {**plain, "ide2": "local-lvm:vm-100-cloudinit"}) == "debian"


def test_without_a_cloudinit_drive_ipconfig0_is_only_a_default(monkeypatch):
    hand_made(extra="ipconfig0: ip=10.30.0.20/16,gw=10.30.0.254\n")    # its prefix and gateway: nothing applies them
    assert pick() == ("10.30.0.20", "config")
    assert pick("10.30.0.30") == ("10.30.0.30", "--ip")
    hand_made(extra="ipconfig0: ip=192.168.1.20/24,gw=192.168.1.1\n")
    with pytest.raises(LabError) as e:
        pick()
    assert str(e.value) == ("wg's ipconfig0 gives 192.168.1.20, outside prod (10.30.0.0/24), and no cloud-init drive "
                            "applies it: give the address wg has on prod with --ip ADDR (recorded only; nothing in "
                            "the VM changes)")
    assert pick("10.30.0.30") == ("10.30.0.30", "--ip")
    agent(monkeypatch, None)
    assert adopting.check_agent(conf_of(), "30", "10.30.0.20", "config", lambda: "stopped") == (
        "10.30.0.20 not checked (wg is not running): pvelab's rules follow the address recorded")
    agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.21")])
    with pytest.raises(LabError, match=r"not 10.30.0.20: .* Give the address it has with --ip, then"):
        adopting.check_agent(conf_of(), "30", "10.30.0.20", "config", lambda: "running")


# --- Task 3: pvelab adopt ---

GOOD = ("ciuser: debian\nipconfig0: ip=10.30.0.20/24,gw=10.30.0.1\nide2: local-lvm:vm-100-cloudinit,media=cdrom\n"
        "cicustom: vendor=local:snippets/qga-vendor.yaml\n")     # pvelab's vendor-data: rename keeps its host keys


@pytest.fixture
def cmd(monkeypatch):
    """The adopt module with its host side faked: quorum, qm status (stopped), the net's rules (refreshed: the
    nets in cmd.refreshed), the guest agent (no answer)."""
    from pvelab.commands import adopt
    adopt.refreshed = []
    monkeypatch.setattr(ui, "YES", True)             # a VM made by hand mostly has no 'clean' snapshot: a warning
    monkeypatch.setattr(adopt, "require_quorum", lambda doing: None)
    monkeypatch.setattr(adopt, "vm_status", lambda vmid: "stopped")
    monkeypatch.setattr(adopt, "refresh_net", lambda n: adopt.refreshed.append(n))
    monkeypatch.setattr(adopt, "ensure_gw_mac", lambda n: adopt.refreshed.append(f"gw {n}"))
    monkeypatch.setattr(adopt, "listening_ports", lambda: set())      # ss: nothing listens
    agent(monkeypatch, None)
    return adopt


def rec(vmid):
    return registry.load()["vms"].get(str(vmid))


def test_adopt_owner_writes_the_record(cmd, capsys):
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--owner", "prod-alice", "--password", "pw"])
    assert rec(100) == {"uuid": "0000-100", "name": "wg", "net": "30", "ip": "10.30.0.20", "user": "debian",
                        "password": "pw", "owner": "alice"}
    assert cmd.refreshed == ["30"]
    out = capsys.readouterr().out
    assert "1. record wg (100, on " in out and "as pvelab's: owner alice's VM (a new owner)" in out
    assert "2. address 10.30.0.20 (its cloud-init ipconfig0), login debian, password as given" in out
    assert "3. prod's rules follow it: blocks" in out
    assert "nothing in the VM changes (no reboot)" in out and "wg is pvelab's now." in out
    assert "pvelab list --net prod" in out


def test_adopt_shared_writes_role_shared(cmd):
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["100", "--shared"])
    assert rec(100) == {"uuid": "0000-100", "name": "wg", "net": "30", "ip": "10.30.0.20", "user": "debian",
                        "role": "shared"}


def test_adopt_changes_nothing_in_the_vm(cmd, monkeypatch):
    hand_made(extra=GOOD)
    path = pathlib.Path(inventory.conf_path(100))
    before = path.read_bytes()
    monkeypatch.setattr(cmd, "vm_status", lambda vmid: "running")
    asked = agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.20")])
    cmd.cmd_adopt(["wg", "--owner", "alice"])      # any other host command fails the test (conftest)
    assert path.read_bytes() == before
    assert asked == [("cmd", 100, "network-get-interfaces")]


def test_adopt_dry_run_shows_the_plan_and_writes_nothing(cmd, capsys):
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--shared"], dry=True)
    out = capsys.readouterr().out
    assert "This will:" in out and "prod's shared VM, which every owner of prod reaches" in out
    assert "dry run - nothing was changed" in out
    assert not os.path.exists(registry.REGISTRY) and cmd.refreshed == []


def test_adopted_vm_counts_everywhere(cmd):
    hand_made(extra=GOOD.replace("10.30.0.20", "10.30.0.11"))
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    conf = inventory.read_conf(100)
    assert inventory.is_managed(conf) and 100 in inventory.on_net("30", inventory.inventory())
    row = credentials.cred_rows()[100]
    assert row == {"name": "wg", "user": "debian", "password": "", "net": "30", "ip": "10.30.0.11"}
    assert owners.owner_of(conf) == "alice" and not owners.is_shared_vm(conf)
    assert networks.next_ip("30", credentials.cred_rows()) == "10.30.0.12"
    hand_made(101, "box", extra=GOOD.replace("10.30.0.20", "10.30.0.12"))
    cmd.cmd_adopt(["box", "--shared"])
    assert isolation.shared_ip("30") == "10.30.0.12"
    assert owners.by_owner(inventory.on_net("30", inventory.inventory())) == {"alice": [100]}
    assert inventory.targets(["--net", "prod"]) == [100, 101]


def legacy_vm():
    vm(100, "#managed-by=lab.sh net=30\nname: wg\ntags: pvelab\nnet0: virtio=BC:24:11:00:00:05,bridge=vmbr30\n"
            "smbios1: uuid=0000-100\n")


def no_quorum(monkeypatch, adopt):
    def stop(doing):
        raise LabError(f"the cluster has no quorum (b is offline), so PVE refuses changes: {doing}")
    monkeypatch.setattr(adopt, "require_quorum", stop)
    hand_made(extra=GOOD)


def gateway_elsewhere(monkeypatch, adopt):
    monkeypatch.setattr(adopting, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(adopting, "gateway_elsewhere", lambda n: "b")
    hand_made(extra=GOOD)


def agent_disagrees(monkeypatch, adopt):
    monkeypatch.setattr(adopt, "vm_status", lambda vmid: "running")
    agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.21")])
    hand_made(extra=GOOD)


OWNER = ["100", "--owner", "alice"]
REFUSALS = {                                         # (setup, argv, the error): nothing written
    "template": (lambda mp, a: hand_made(extra=GOOD + "template: 1\n"), OWNER,
                 "wg (100) is a template: pvelab adopt takes VMs"),
    "record": (lambda mp, a: record(100, "wg"), OWNER, "wg (100) is pvelab's already (pvelab list shows it)"),
    "legacy": (lambda mp, a: legacy_vm(), OWNER, "wg (100) was made by an older pvelab: pvelab migrate records it"),
    "no uuid": (lambda mp, a: vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr30\n" + GOOD), OWNER,
                "wg (100) has no smbios1 uuid, which pvelab's record is matched by: qm set 100 --smbios1 "
                "uuid=$(cat /proc/sys/kernel/random/uuid), then run pvelab adopt again"),
    "no name": (lambda mp, a: vm(100, f"net0: virtio={MAC},bridge=vmbr30\nsmbios1: uuid=1\n" + GOOD), OWNER,
                "VM 100 has no name: qm set 100 --name NAME, then run pvelab adopt again"),
    "net2 as name": (lambda mp, a: hand_made(name="net2", extra=GOOD), OWNER,
                     "net2: 'net2' means something else to pvelab (all VMs, a net, a bridge) - qm set 100 --name NEW, "
                     "then run pvelab adopt again"),
    "pvelab's name": (lambda mp, a: (record(201, "wg"), hand_made(extra=GOOD)), OWNER,
                      "VM name wg is pvelab's VM 201 already: give one of them another name first (pvelab rename, or "
                      "qm set 100 --name NEW)"),
    "vmbr0": (lambda mp, a: hand_made(bridge="vmbr0", extra=GOOD), OWNER,
              "wg's net0 is on vmbr0, not on one of pvelab's nets (prod, other): pvelab adopt takes a VM whose net0 "
              "is on its net"),
    "gateway elsewhere": (gateway_elsewhere, OWNER, "prod's gateway is on b: its rules (isolation, ssh forwards, "
                                                    "blocks) are there - run pvelab adopt on that host"),
    "no quorum": (no_quorum, OWNER,
                  "the cluster has no quorum (b is offline), so PVE refuses changes: no VM can be adopted"),
    "shared taken": (lambda mp, a: (record(210, "box", role="shared"), hand_made(extra=GOOD)), ["100", "--shared"],
                     "box is prod's shared VM already (one per net: they share a fixed MAC) - --owner NAME adopts wg "
                     "as an owner's VM"),
    "firewall": (lambda mp, a: hand_made(extra=GOOD, firewall=1), OWNER,
                 "wg's net0 has PVE's firewall on (firewall=1): its port on vmbr30 is then fwpr100p0, not tap100i0, "
                 "so pvelab's isolation would not know it. qm set 100 --net0 virtio=BC:24:11:AA:BB:CC,bridge=vmbr30 "
                 "(PVE applies it at once while it runs, with network hotplug on - its default; else at its next "
                 "start), then run pvelab adopt again"),
    "other host, bridge net": (lambda mp, a: hand_made(extra=GOOD, node=two_hosts()), OWNER,
                               "wg is on b; prod is a bridge on {here} only: move it there (qm migrate), then run "
                               "pvelab adopt again"),
    "outside the zone": (lambda mp, a: (sdn_prod([nodes.this_node()]),
                                        hand_made(bridge="lab30", extra=GOOD, node=two_hosts())), OWNER,
                         "wg is on b; prod's zone LAB2 is on {here} only: move it to one of those hosts (qm migrate), "
                         "then run pvelab adopt again"),
    "vlan tag": (lambda mp, a: vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr30,tag=5\nsmbios1: uuid=0000-100\n"
                                        + GOOD), OWNER,
                 "wg's net0 is on vmbr30 with VLAN tag 5, so not on prod's network (untagged): qm set 100 --net0 "
                 "virtio=BC:24:11:AA:BB:CC,bridge=vmbr30 (the VM is then on prod, its address one of prod's), then "
                 "run pvelab adopt again"),
    "agent disagrees": (agent_disagrees, OWNER,
                        "wg's guest agent reports 10.30.0.21 on net0, not 10.30.0.20 (its ipconfig0's): pvelab's rules "
                        "would follow an address it does not have. Give ipconfig0 the address it has (qm set 100 "
                        "--ipconfig0 ip=10.30.0.21/24,gw=10.30.0.1; cloud-init applies it at its next reboot), or fix "
                        "the guest's own network config, then run pvelab adopt again"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_adopt_refusals(cmd, monkeypatch, case):
    setup, argv, text = REFUSALS[case]
    setup(monkeypatch, cmd)
    had = registry.load()
    with pytest.raises(LabError) as e:
        cmd.cmd_adopt(argv)
    assert str(e.value) == text.replace("{here}", nodes.this_node())
    assert registry.load() == had and cmd.refreshed == []


def test_adopt_exposed_net_warns_about_ssh_from_outside(cmd, monkeypatch, capsys):
    monkeypatch.setattr(ui, "YES", False)
    monkeypatch.setattr(cmd, "is_exposed", lambda n: True)
    hand_made(extra=GOOD)
    with pytest.raises(LabError, match="not continuing without a yes"):
        cmd.cmd_adopt(["wg", "--owner", "alice"])
    out = capsys.readouterr().out
    assert "prod's rules follow it: blocks, ssh port 22100 from outside" in out
    assert "! prod is exposed: wg's ssh becomes reachable from outside on port 22100" in out
    assert rec(100) is None


def test_adopt_warns_about_what_pvelab_cant_follow(cmd, monkeypatch, capsys):
    monkeypatch.setattr(ui, "YES", True)
    vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr30\nnet1: virtio=BC:24:11:00:00:09,bridge=vmbr0\n"
            "smbios1: uuid=0000-100\nipconfig0: ip=dhcp\n")
    cmd.cmd_adopt(["wg", "--owner", "alice", "--user", "admin", "--ip", "10.30.0.30"])
    out = capsys.readouterr().out
    assert "address 10.30.0.30 (as given), login admin, password none recorded (pvelab passwd wg sets one)" in out
    assert "! 10.30.0.30 not checked (wg is not running): pvelab's rules follow the address recorded" in out
    assert ("! wg has no cloud-init drive: pvelab rename and net set change its config only (its hostname and address "
            "are changed in the VM by hand), yet while it runs they reboot it and wait for cloud-init up to 15 min - "
            "stop it first; pvelab passwd sets a password only while it runs") in out
    assert "vendor-data" not in out                  # no cloud-init drive: said once
    assert "! wg also has net1 on vmbr0: pvelab's rules cover net0 only" in out
    assert rec(100)["ip"] == "10.30.0.30" and rec(100)["user"] == "admin"
    registry.update({100: None}, {})
    hand_made(extra="ciuser: debian\nipconfig0: ip=10.30.0.20/24,gw=10.30.0.1\n")     # no drive: defaults only
    cmd.cmd_adopt(["wg", "--owner", "alice", "--user", "admin", "--ip", "10.30.0.30"])
    assert rec(100)["ip"] == "10.30.0.30" and rec(100)["user"] == "admin"
    registry.update({100: None}, {})
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    out = capsys.readouterr().out
    assert "address 10.30.0.20 (its ipconfig0, which no cloud-init drive applies), login debian" in out
    assert "! 10.30.0.20 not checked (wg is not running)" in out


def test_adopt_owner_with_vms_says_so(cmd, capsys):
    record(201, "prod-alice", owner="alice")
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    assert "owner alice's VM (with their 1 VM(s) on prod)" in capsys.readouterr().out


def test_adopt_finds_the_vm(cmd):
    hand_made(extra=GOOD)
    hand_made(101, "web", extra=GOOD)
    hand_made(102, "web", extra=GOOD)
    inv = inventory.inventory()
    assert cmd.find_vm("wg", inv) == 100 and cmd.find_vm("101", inv) == 101
    with pytest.raises(LabError, match=r"^no VM called 'wgg' - did you mean wg\?$"):
        cmd.find_vm("wgg", inv)
    with pytest.raises(LabError, match=r"^several VMs are called web \(101, 102\): give its VMID$"):
        cmd.find_vm("web", inv)


@pytest.mark.parametrize("argv, text", [
    ([], "pvelab adopt VM --shared | --owner NAME [--user USER] [--password PW] [--ip ADDR]"),
    (["a", "b", "--shared"], "pvelab adopt VM --shared | --owner NAME [--user USER] [--password PW] [--ip ADDR]"),
    (["a", "--nope"], "pvelab adopt VM --shared | --owner NAME [--user USER] [--password PW] [--ip ADDR]"),
    (["a", "--shared", "--owner", "x"], "--shared or --owner NAME: one of them"),
    (["a"], "whose is it? --shared (the net's shared VM) or --owner NAME"),
    (["a", "--shared", "--password", "a\tb"], "--password: a password, without tabs or line breaks"),
])
def test_adopt_usage(cmd, argv, text):
    with pytest.raises(UsageError) as e:
        cmd.cmd_adopt(argv)
    assert str(e.value) == text


def two_cards(**wg):
    """alice's VMs: wg (100, adopted: no password; wg's other fields) and prod-alice (201, password pw)."""
    record(100, "wg", owner="alice", **{"password": "", **wg})
    record(201, "prod-alice", ip="10.30.0.12", owner="alice")


def handout_page(tmp_path):
    out = tmp_path / "cards.html"
    handout.cmd_handout(["--net", "prod", "-o", str(out)])
    return out.read_text()


def test_handout_card_shows_a_password_only_when_every_vm_has_it(tmp_path, capsys):
    two_cards()                                      # wg's unknown: the card's password may not work there
    assert "Password: <code>-</code>" in handout_page(tmp_path)
    assert ("alice's VMs on prod: no password recorded for wg (its login card shows none) - pvelab passwd wg gives "
            "them one password") in capsys.readouterr().err
    two_cards(password="pw")
    assert "Password: <code>pw</code>" in handout_page(tmp_path)
    assert "login card shows none" not in capsys.readouterr().err
    two_cards(password="other")
    assert "Password: <code>-</code>" in handout_page(tmp_path)
    assert ("alice's VMs on prod have different passwords (its login card shows none) - pvelab passwd wg gives them "
            "one password") in capsys.readouterr().err
    two_cards()
    registry.update({201: None}, {})                 # wg alone, none recorded
    assert "Password: <code>-</code>" in handout_page(tmp_path)
    assert ("alice's VMs on prod: no password recorded (its login card shows none) - pvelab passwd wg gives them one "
            "password") in capsys.readouterr().err
    record(100, "prod-shared", password="", role="shared")
    handout_page(tmp_path)
    assert ("prod-shared on prod: no password recorded (its login card shows none) - pvelab passwd prod-shared sets "
            "one") in capsys.readouterr().err


def test_group_logins():
    rows = {100: {"user": "admin", "password": ""}, 201: {"user": "student", "password": "pw"},
            202: {"user": "other", "password": "pw2"}}
    login = owners.group_logins(rows, [202, 201, 100])
    assert (login.user, login.password, login.missing, login.differ) == ("student", "pw", [100], True)
    assert login.users == {"admin", "student", "other"} and login.card_password is None and login.mixed
    login = owners.group_logins(rows, [100])
    assert (login.user, login.password, login.card_password, login.mixed) == ("admin", None, None, False)
    rows[100]["password"] = "pw"
    assert owners.group_logins(rows, [100, 201]).card_password == "pw"


def test_handout_card_shows_each_vms_user_when_they_differ(tmp_path, capsys):
    two_cards(user="admin", password="pw")
    page = handout_page(tmp_path)
    assert "User: <code>" not in page
    assert "user <code>admin</code>" in page and "user <code>student</code>" in page
    assert "Password: <code>pw</code>" in page
    two_cards(password="pw")
    page = handout_page(tmp_path)
    assert "User: <code>student</code>" in page and "user <code>" not in page


def test_warn_layout_warns_on_missing_and_different_logins(capsys):
    two_cards()                                      # wg: none recorded
    inv = inventory.inventory()
    owner.warn_layout({"30": {100: (inv[100], "alice")}}, inv)
    assert ("alice's VMs on prod: no password recorded for wg (one login card shows none) - pvelab passwd wg gives "
            "them one password") in capsys.readouterr().err
    two_cards(password="other")
    owner.warn_layout({"30": {100: (inv[100], "alice")}}, inventory.inventory())
    assert ("alice's VMs on prod have different passwords (one login card shows none) - pvelab passwd wg gives them "
            "one password") in capsys.readouterr().err
    two_cards(user="admin", password="pw")
    owner.warn_layout({"30": {100: (inv[100], "alice")}}, inventory.inventory())
    err = capsys.readouterr().err
    assert "alice's VMs on prod log in as admin, student (the login card shows each VM's user)" in err
    assert "password" not in err
    two_cards(password="pw")
    owner.warn_layout({"30": {100: (inv[100], "alice")}}, inventory.inventory())
    assert "alice" not in capsys.readouterr().err
    record(100, "wg", owner="alice", password="")
    record(201, "prod-alice", ip="10.30.0.12", owner="alice", password="")      # none recorded at all: the card
    owner.warn_layout({"30": {100: (inv[100], "alice")}}, inventory.inventory())  # says so, not owner
    assert "alice" not in capsys.readouterr().err


def test_list_password_dash_when_none_recorded(monkeypatch, capsys):
    fake_list(monkeypatch)
    record(201, "prod-alice", password="")
    listing.cmd_list(["--passwords"])
    assert capsys.readouterr().out.splitlines()[1].split()[-1] == "-"
    listing.cmd_list(["--ssh"])
    assert capsys.readouterr().out.splitlines()[1].split()[-1] == "-"


def test_list_template_column_dash_when_none_recorded(monkeypatch, capsys):
    fake_list(monkeypatch)
    record(201, "prod-alice")
    listing.cmd_list([])
    row = capsys.readouterr().out.splitlines()[1]
    assert row.split()[:3] == ["prod-alice", "201", "-"]


# --- review fixes ---

def sdn_prod(zone_nodes=None):
    """Net 30 prod as pvelab's SDN vnet lab30 (its gateway here), net 31 other as before; zone_nodes: the
    hosts of lab30's zone LAB2 (None: no zone set, every host)."""
    with open(config.IFACES, "w") as f:
        f.write("# lab.sh net 30 begin (managed by lab)\n        # pvelab name prod\niface lab30 inet static\n"
                "        address 10.30.0.1/24\n# lab.sh net 30 end\n" + net_block("31", "10.31.0.1/24", "other"))
    if zone_nodes:
        with open(config.SDN_VNETS, "w") as f:
            f.write("vnet: lab30\n\tzone LAB2\n")
        with open(config.SDN_ZONES, "w") as f:
            f.write(f"vxlan: LAB2\n\tnodes {','.join(zone_nodes)}\n")


def two_hosts():
    """A cluster of this host and b (both online, quorate); returns b."""
    here = nodes.this_node()
    with open(nodes.MEMBERS, "w") as f:
        json.dump({"nodename": here, "version": 4, "cluster": {"name": "c", "quorate": 1},
                                 "nodelist": {here: {"id": 1, "online": 1, "ip": "10.0.0.123"},
                                              "b": {"id": 2, "online": 1, "ip": "10.0.0.229"}}}, f)
    return "b"


def test_strays_offer_no_vm_on_a_host_the_net_is_not_on():
    here, there = nodes.this_node(), two_hosts()
    hand_made(extra=GOOD, node=there)                # vmbr30 on b: another network
    assert adopting.strays(inventory.inventory()) == [] and inventory.lab_data(strays=True)["strays"] == []
    sdn_prod([here])
    hand_made(bridge="lab30", extra=GOOD, node=there)
    assert adopting.strays(inventory.inventory()) == []
    sdn_prod([here, there])
    assert [s.vmid for s in adopting.strays(inventory.inventory())] == [100]


def test_adopt_sdn_vm_on_another_host_fixes_the_gateway_mac(cmd):
    sdn_prod()                                       # no zone set: every host
    hand_made(bridge="lab30", extra=GOOD, node=two_hosts())
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    assert rec(100)["net"] == "30"
    assert cmd.refreshed == ["gw 30", "30"]


def test_adopt_sdn_vm_here_leaves_the_gateway_mac(cmd):
    sdn_prod()
    two_hosts()
    hand_made(bridge="lab30", extra=GOOD)
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    assert cmd.refreshed == ["30"]


def test_adopt_warns_what_rename_and_net_set_do_to_it(cmd, monkeypatch, capsys):
    monkeypatch.setattr(ui, "YES", True)
    hand_made(extra=GOOD.replace("cicustom: vendor=local:snippets/qga-vendor.yaml\n", ""))   # PVE's web UI: none
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    out = capsys.readouterr().out
    assert ("! wg has no pvelab vendor-data (cicustom vendor=local:snippets/qga-vendor.yaml): cloud-init in it makes "
            "new SSH host keys (its users get a host key warning) when pvelab rename or net set reboots it while it "
            "runs, and at its next boot or rollback after pvelab passwd (a new cloud-init password in its config "
            "and snapshots)") in out
    registry.update({100: None}, {})
    hand_made(extra=GOOD.replace("vendor=", "user=local:snippets/u.yaml,vendor="))    # with its own user-data too
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    assert "vendor-data" not in capsys.readouterr().out


def test_adopt_rules_failure_names_what_brings_them_up_to_date(cmd, monkeypatch, capsys):
    monkeypatch.setattr(ui, "YES", True)

    def fail(n):
        raise LabError("iptables: no chain")
    monkeypatch.setattr(cmd, "refresh_net", fail)
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--owner", "alice"])        # prod: neither isolated nor exposed
    err = capsys.readouterr().err
    assert rec(100)["owner"] == "alice"                # the record stays
    assert "wg is recorded; prod's rules not brought up to date (iptables: no chain)\n" in err
    assert "pvelab isolate" not in err and "pvelab expose" not in err
    registry.update({100: None}, {})
    monkeypatch.setattr(cmd, "is_isolated", lambda n: True)
    monkeypatch.setattr(cmd, "is_exposed", lambda n: True)
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    assert ("wg is recorded; prod's rules not brought up to date (iptables: no chain) - once that is fixed: pvelab "
            "expose --net prod; pvelab isolate --net prod") in capsys.readouterr().err


def test_lab_data_offers_the_strays_only_when_asked():
    hand_made(extra=GOOD)
    hand_made(101, "web", extra="ipconfig0: ip=dhcp\n")
    vm(106, "name: two\nnet0: virtio=BC:24:11:00:00:06,bridge=vmbr0\nnet1: virtio=BC:24:11:00:00:07,bridge=vmbr30\n")
    record(201, "prod-alice")
    assert inventory.lab_data(strays=True)["strays"] == ["wg", "web"]          # net1 on prod: adopt takes no two
    assert inventory.lab_data()["strays"] == []


# --- review round 1 ---

def test_firewall_card_is_still_a_stray():
    hand_made(firewall=1)                            # PVE's web UI turns the firewall on by default
    assert [s.vmid for s in adopting.strays(inventory.inventory())] == [100]


def passwd_world(monkeypatch, answers):
    """passwd on alice's wg (100, adopted: no password recorded) and prod-alice (201, pw), both running; the
    guest agent's answer to the password check: answers[vmid]."""
    from pvelab.commands import passwd
    rows = {100: {"name": "wg", "net": "30", "user": "debian", "password": "", "ip": "10.30.0.20"},
            201: {"name": "prod-alice", "net": "30", "user": "student", "password": "pw", "ip": "10.30.0.11"}}
    monkeypatch.setattr(passwd, "cred_rows", lambda: rows)
    monkeypatch.setattr(passwd, "targets", lambda a: [100, 201])
    monkeypatch.setattr("pvelab.owners.read_conf", lambda i: {"name": rows[i]["name"], "description":
                        "managed-by=lab.sh net=30 owner=alice"})
    monkeypatch.setattr(passwd, "vm_statuses", lambda ids: {i: "running" for i in ids})
    monkeypatch.setattr(passwd, "node_of", lambda i: "workshop1")
    monkeypatch.setattr(passwd, "agent_out", lambda vmid, *a, **k: answers[vmid])
    return passwd


def test_passwd_takes_no_password_recorded_as_unknown(monkeypatch, capsys):
    passwd = passwd_world(monkeypatch, {100: "changed", 201: "same"})
    passwd.cmd_passwd(["--check", "wg", "prod-alice"])
    out = capsys.readouterr().out
    assert "wg" in out and "no password recorded" in out.splitlines()[0]
    assert "changed in the VM" not in out
    passwd.cmd_passwd(["wg", "--keep-changed"], dry=True)    # wg's unknown one: not a password its user chose
    out = capsys.readouterr().out
    assert "new password for 1 owner(s), 2 VM(s)" in out and "set in the VM now (running): 2 VM(s)" in out
    assert "left alone" not in out and "the user changed" not in out


def test_guest_problem_names_adopt_only_when_adopt_takes_it():
    plain = "guest 100 (not a pvelab VM) has a network card on {}"
    hand_made(bridge="vmbr40")                       # not one of pvelab's nets (a PVE vnet about to be taken over)
    assert networks.guest_problem("vmbr40") == plain.format("vmbr40")
    vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr0\nnet1: virtio=BC:24:11:00:00:01,bridge=vmbr30\n")
    assert networks.guest_problem("vmbr30") == plain.format("vmbr30")          # net1: adopt takes net0's
    hand_made(extra="template: 1\n")
    assert networks.guest_problem("vmbr30") == plain.format("vmbr30")
    vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr0\n[clean]\nnet0: virtio={MAC},bridge=vmbr30\n")
    assert networks.guest_problem("vmbr30") == plain.format("vmbr30")          # in a snapshot only
    hand_made()
    assert networks.guest_problem("vmbr30") == plain.format("vmbr30") + (": pvelab adopt 100 --shared|--owner NAME "
                                                                         "hands it over, or take its card off vmbr30")
    d = pathlib.Path(nodes.NODES_DIR) / nodes.this_node() / "lxc"
    d.mkdir(parents=True)
    (d / "300.conf").write_text("net0: name=eth0,bridge=vmbr31\n")                # a container: no adopt
    assert networks.guest_problem("vmbr31") == "guest 300 (not a pvelab VM) has a network card on vmbr31"


def test_guest_problem_says_why_adopt_would_not_take_it(monkeypatch):
    head = "guest 100 (not a pvelab VM) has a network card on vmbr30: "
    hand_made(firewall=1)
    assert networks.guest_problem("vmbr30") == (
        head + "pvelab adopt would hand it over, but wg's net0 has PVE's firewall on (firewall=1): its port on vmbr30 "
        "is then fwpr100p0, not tap100i0, so pvelab's isolation would not know it. qm set 100 --net0 "
        "virtio=BC:24:11:AA:BB:CC,bridge=vmbr30 (PVE applies it at once while it runs, with network hotplug on - its "
        "default; else at its next start), then run pvelab adopt again; or take its card off vmbr30")
    here, there = nodes.this_node(), two_hosts()
    hand_made(node=there)
    pathlib.Path(nodes.NODES_DIR, here, "qemu-server", "100.conf").unlink()
    assert networks.guest_problem("vmbr30") == (
        head + f"pvelab adopt would hand it over, but wg is on {there}; prod is a bridge on {here} only: move it there "
        "(qm migrate), then run pvelab adopt again; or take its card off vmbr30")


def test_a_card_with_a_vlan_tag_is_not_on_the_net():
    vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr30,tag=5\n")
    vm(101, f"name: two\nnet0: virtio={MAC},bridge=vmbr0\nnet1: virtio=BC:24:11:00:00:01,bridge=vmbr30,tag=5\n")
    assert adopting.strays(inventory.inventory()) == []
    assert networks.guest_problem("vmbr30").endswith("has a network card on vmbr30")       # no adopt named


def test_a_name_of_digits_is_offered_by_its_vmid():
    hand_made(100, "123")                            # pvelab adopt 123 would read VMID 123
    assert [s.arg for s in adopting.strays(inventory.inventory())] == ["100"]


def test_strays_leave_out_nets_whose_gateway_is_elsewhere(monkeypatch, capsys):
    fake_list(monkeypatch)
    hand_made()
    hand_made(101, "web", bridge="vmbr31")
    monkeypatch.setattr(adopting, "our_nets", lambda text=None: ["31"])     # prod's gateway: another host
    assert [s.vmid for s in adopting.strays(inventory.inventory())] == [101]
    assert inventory.lab_data(strays=True)["strays"] == ["web"]
    listing.cmd_list([])
    out = capsys.readouterr().out
    assert "wg (100)" not in out and "web (101) on other" in out


def test_strays_look_at_every_card(monkeypatch):
    vm(100, f"name: two\nnet0: virtio={MAC},bridge=vmbr30\nnet1: virtio=BC:24:11:00:00:01,bridge=vmbr31\n")
    found = adopting.strays(inventory.inventory(), only_net="31")         # net0 on prod: --net other names net1
    assert [(s.vmid, s.net, s.nic) for s in found] == [(100, "31", "net1")]
    assert [(s.net, s.nic) for s in adopting.strays(inventory.inventory())] == [("30", "net0")]
    monkeypatch.setattr(adopting, "our_nets", lambda text=None: ["31"])     # prod's gateway: another host
    assert [(s.net, s.nic) for s in adopting.strays(inventory.inventory())] == [("31", "net1")]


def test_no_stray_search_without_a_vm_made_by_hand(monkeypatch, capsys):
    fake_list(monkeypatch)
    record(201, "prod-alice")
    hand_made(103, "tpl", extra="template: 1\n")

    def search(text=None):
        raise AssertionError("pvelab's bridges read")
    monkeypatch.setattr(adopting, "our_nets", search)
    assert adopting.strays(inventory.inventory()) == []
    assert inventory.lab_data(strays=True)["strays"] == []
    listing.cmd_list([])
    assert "prod-alice" in capsys.readouterr().out


def test_strays_read_no_nets_or_zones_for_a_vm_on_none_of_their_bridges(monkeypatch):
    hand_made(bridge="vmbr0")                        # an admin's bridge: no net, no zone to read

    def read(*a, **k):
        raise AssertionError("nets or zones read")
    monkeypatch.setattr(adopting.nets, "all_nets", read)
    monkeypatch.setattr(adopting.zones, "net_hosts", read)
    assert adopting.strays(inventory.inventory()) == []
    hand_made()                                      # on vmbr30, prod's: its hosts asked, all_nets still not
    monkeypatch.setattr(adopting.zones, "net_hosts", lambda n: None)
    assert [s.vmid for s in adopting.strays(inventory.inventory())] == [100]


def test_pick_ip_clashes_only_on_the_same_network():
    there = two_hosts()
    hand_made()
    hand_made(101, "far", extra="ipconfig0: ip=10.30.0.40/24,gw=10.30.0.1\n", node=there)   # vmbr30 on b: another network
    assert pick("10.30.0.40") == ("10.30.0.40", "--ip")
    sdn_prod([nodes.this_node(), there])             # lab30's zone spans both hosts: one network
    hand_made(bridge="lab30")
    hand_made(101, "far", bridge="lab30", extra="ipconfig0: ip=10.30.0.40/24,gw=10.30.0.1\n", node=there)
    with pytest.raises(LabError, match="^10.30.0.40 is far's address already"):
        pick("10.30.0.40")


def test_adopt_warns_about_a_disk_not_scsi0_and_no_clean_snapshot(cmd, capsys):
    hand_made(extra=GOOD + "virtio0: local-lvm:vm-100-disk-0,size=20G\n[first]\nname: wg\n")
    cmd.cmd_adopt(["wg", "--owner", "alice"], dry=True)
    out = capsys.readouterr().out
    assert ("! wg's disk is virtio0, not scsi0: pvelab resize --disk and pvelab list's size column go by scsi0 "
            "(list shows 0G) - qm disk resize 100 virtio0 SIZE resizes it") in out
    assert ("! wg has no 'clean' snapshot, which pvelab rollback goes back to by default (its snapshots: first): "
            "pvelab snapshot wg takes it now, or pvelab rollback wg --to NAME names one") in out
    hand_made(extra=GOOD + "scsi0: local-lvm:vm-100-disk-0,size=20G\n[clean]\nname: wg\n")
    cmd.cmd_adopt(["wg", "--owner", "alice"], dry=True)
    out = capsys.readouterr().out
    assert "scsi0" not in out and "snapshot" not in out


def test_adopt_into_an_owner_with_a_password_warns_without_one(cmd, capsys):
    record(201, "prod-alice", owner="alice")         # password pw
    hand_made(extra=GOOD.replace("ciuser: debian", "ciuser: student"))
    cmd.cmd_adopt(["wg", "--owner", "alice"], dry=True)
    assert ("! alice's VMs on prod: no password recorded for wg (their login card shows none) - pvelab passwd wg "
            "gives them one password") in capsys.readouterr().out
    cmd.cmd_adopt(["wg", "--owner", "alice", "--password", "pw"], dry=True)
    assert "login card" not in capsys.readouterr().out


def test_adopt_with_a_password_into_an_owner_without_one_warns(cmd, capsys):
    record(201, "prod-alice", owner="alice", password="")
    hand_made(extra=GOOD.replace("ciuser: debian", "ciuser: student"))
    cmd.cmd_adopt(["wg", "--owner", "alice", "--password", "pw"], dry=True)
    assert ("! alice's VMs on prod: no password recorded for prod-alice (their login card shows none) - pvelab "
            "passwd wg gives them one password") in capsys.readouterr().out
    record(201, "prod-alice", owner="alice", password="other")
    cmd.cmd_adopt(["wg", "--owner", "alice", "--password", "pw"], dry=True)
    assert ("! alice's VMs on prod have different passwords (their login card shows none) - pvelab passwd wg gives "
            "them one password") in capsys.readouterr().out


def test_adopt_with_another_login_user_warns(cmd, capsys):
    record(201, "prod-alice", owner="alice")         # user student
    hand_made(extra=GOOD)                            # ciuser debian
    cmd.cmd_adopt(["wg", "--owner", "alice", "--password", "pw"], dry=True)
    assert ("! wg logs in as debian, alice's other VMs on prod as student: their login card shows each VM's user - "
            "or adopt wg under another --owner") in capsys.readouterr().out


# --- review round 2 ---

def test_passwd_refuses_a_vm_it_cannot_reach(monkeypatch, capsys):
    passwd = passwd_world(monkeypatch, {100: None, 201: "same"})     # wg: no guest agent answer
    monkeypatch.setattr(ui, "YES", True)
    set_ci = []
    monkeypatch.setattr(passwd, "set_cipassword", lambda i, pw, node, current: set_ci.append(i))
    monkeypatch.setattr(passwd, "set_in_vm", lambda *a: None)
    saved = []
    monkeypatch.setattr(passwd, "cred_save", lambda rows: saved.append({i: r["password"] for i, r in rows.items()}))
    passwd.cmd_passwd(["wg"], dry=True)              # wg (no cloud-init drive) is stopped or its agent is silent
    out = capsys.readouterr().out
    assert ("! wg has no cloud-init drive, so a password in PVE's config never reaches it: not changed - start it "
            "with its guest agent running, then pvelab passwd wg again") in out
    assert "they take it at their next start" not in out
    passwd.cmd_passwd(["wg"])
    out = capsys.readouterr().out
    assert set_ci == [201] and saved[0][100] == "" and saved[0][201] != "pw"
    assert "no cloud-init drive" in out


def test_passwd_names_the_recorded_login_when_the_vm_lacks_that_user(monkeypatch, capsys):
    passwd = passwd_world(monkeypatch, {100: "nouser", 201: "same"})    # wg's agent answers: no user debian
    monkeypatch.setattr(ui, "YES", True)
    monkeypatch.setattr(passwd, "set_cipassword", lambda i, pw, node, current: None)
    monkeypatch.setattr(passwd, "set_in_vm", lambda *a: None)
    monkeypatch.setattr(passwd, "cred_save", lambda rows: None)
    passwd.cmd_passwd(["wg"], dry=True)
    out = capsys.readouterr().out
    assert ("! wg has no user debian, the login pvelab adopt recorded: not changed - add that user in the VM "
            "(adduser debian), then pvelab passwd wg again") in out
    assert "no cloud-init drive" not in out and "guest agent did not answer" not in out
    passwd_world(monkeypatch, {100: "nouser", 201: "nouser"})
    monkeypatch.setattr(passwd, "targets", lambda a: [100])
    with pytest.raises(LabError, match="^wg has no user debian, the login pvelab adopt recorded"):
        passwd.cmd_passwd(["wg"], dry=True)


@pytest.mark.parametrize("whose", [["--owner", "alice"], ["--shared"]])
def test_adopt_plan_warns_about_a_name_create_would_take(cmd, capsys, whose):
    hand_made(name="prod-student03", extra=GOOD)
    cmd.cmd_adopt(["prod-student03", *whose], dry=True)          # in the plan, before the yes
    assert ("! prod-student03 is named as student03's VM: pvelab create on prod stops at it - pvelab rename "
            "prod-student03 NEW") in capsys.readouterr().out


def test_adopt_plan_warns_about_the_owners_layout(cmd, capsys):
    sdn_prod()                                       # no zone set: every host
    record(201, "prod-alice", owner="alice")
    record(202, "prod-bob01", ip="10.30.0.12", owner="bob")
    hand_made(bridge="lab30", mac="02:00:0A:0A:1E:02", extra=GOOD, node=two_hosts())     # the shared VM's MAC
    cmd.cmd_adopt(["wg", "--owner", "alice"], dry=True)
    out = capsys.readouterr().out
    assert (f"! alice's VMs on prod are on 2 hosts (b, {nodes.this_node()}): pvelab move them together (pvelab move "
            "VM... --to HOST)") in out
    assert ("! prod: owners have 1 to 2 VMs - pvelab create on it counts VMs per owner (NxM): even them out "
            "first") in out
    cmd.cmd_adopt(["wg", "--owner", "bob"], dry=True)
    out = capsys.readouterr().out
    assert "on 2 hosts (b, " in out and "bob's VMs" in out
    cmd.cmd_adopt(["wg", "--shared"], dry=True)      # no owner's: neither
    out = capsys.readouterr().out
    assert "hosts" not in out and "owners have" not in out


def test_adopt_exposed_net_says_when_the_ssh_port_cannot_be_forwarded(cmd, monkeypatch, capsys):
    monkeypatch.setattr(cmd, "is_exposed", lambda n: True)
    monkeypatch.setattr(cmd, "listening_ports", lambda: {22100})
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--owner", "alice"], dry=True)
    out = capsys.readouterr().out
    assert "prod's rules follow it: blocks\n" in out and "ssh port 22100 from outside" not in out
    assert ("! prod is exposed, but port 22100 is used by a service on this host: wg's ssh is not reachable from "
            "outside") in out
    hand_made(50000, "big", extra=GOOD.replace("10.30.0.20", "10.30.0.21"))
    cmd.cmd_adopt(["big", "--owner", "bob"], dry=True)
    assert ("! prod is exposed, but port 72000 doesn't exist: big's ssh is not reachable from outside"
            in capsys.readouterr().out)
