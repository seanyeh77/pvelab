"""pvelab adopt: a VM made by hand (PVE's web UI) on one of pvelab's nets becomes pvelab's; pvelab list names
such VMs. No host commands: qm and the guest agent are faked."""
import pytest

from pvelab import adopting, config, inventory, networks, nodes, registry
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
    d = __import__("pathlib").Path(nodes.NODES_DIR) / (node or nodes.this_node()) / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(text)


def hand_made(vmid=100, name="wg", bridge="vmbr30", mac=MAC, extra=""):
    vm(vmid, f"name: {name}\nnet0: virtio={mac},bridge={bridge},firewall=1\n"
             f"smbios1: uuid=0000-{vmid}\n" + extra)


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
    assert adopting.stray_line(wg, inv, set()) == ("wg (100) on prod: not made by pvelab, so pvelab leaves it alone; "
                                                   "pvelab adopt wg --shared (or --owner NAME) hands it over")
    assert adopting.stray_line(wg, inv, {"30"}) == ("wg (100) on prod: not made by pvelab, so pvelab leaves it alone; "
                                                    "pvelab adopt wg --owner NAME hands it over")
    assert adopting.stray_line(two, inv, set()) == ("two (106) on prod (net1): not made by pvelab, so pvelab leaves it "
                                                    "alone; pvelab adopt takes a VM whose net0 is on its net")
    hand_made(107, "wg")                             # a name used twice: the VMID
    inv = inventory.inventory()
    assert "pvelab adopt 100 --shared" in adopting.stray_line(adopting.strays(inv)[0], inv, set())


def fake_list(monkeypatch):
    from pvelab.commands import listing
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
    listing = fake_list(monkeypatch)
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
    listing = fake_list(monkeypatch)
    monkeypatch.setattr(listing, "exposure", lambda ns: ({"30"}, set()))
    monkeypatch.setattr(listing, "public_host", lambda: "203.0.113.5")
    hand_made(100)
    record(201, "prod-alice")
    listing.cmd_list(["--exposed"])
    out = capsys.readouterr().out
    assert "prod-alice" in out and "wg (100)" not in out


def test_list_leaves_out_shared_when_the_net_has_one(monkeypatch, capsys):
    listing = fake_list(monkeypatch)
    hand_made(100)
    record(210, "prod-shared", role="shared")
    listing.cmd_list([])
    assert "pvelab adopt wg --owner NAME hands it over" in capsys.readouterr().out


def test_guest_problem_points_to_adopt():
    hand_made(100)
    assert networks.guest_problem("vmbr30") == ("guest 100 (not a pvelab VM) has a network card on vmbr30: pvelab "
                                                "adopt 100 --shared|--owner NAME hands it over, or take its card off "
                                                "vmbr30")
    d = __import__("pathlib").Path(nodes.NODES_DIR) / nodes.this_node() / "lxc"
    d.mkdir(parents=True)
    (d / "300.conf").write_text("net0: name=eth0,bridge=vmbr31\n")
    assert networks.guest_problem("vmbr31") == "guest 300 (not a pvelab VM) has a network card on vmbr31"


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
    asked = agent(monkeypatch, [iface("00:00:00:00:00:00", "127.0.0.1", name="lo"),
                                iface("bc:24:11:aa:bb:cc", "10.30.0.20", "10.30.0.21")])
    assert adopting.guest_addresses(100, "bc:24:11:aa:bb:cc", "a") == ["10.30.0.20", "10.30.0.21"]
    assert asked == [("cmd", 100, "network-get-interfaces")]
    assert adopting.guest_addresses(100, "bc:24:11:00:00:01", "a") is None      # not reported
    agent(monkeypatch, None)
    assert adopting.guest_addresses(100, "bc:24:11:aa:bb:cc", "a") is None


def test_pick_ip_takes_ipconfig0_in_range():
    hand_made(extra="ipconfig0: ip=10.30.0.20/24,gw=10.30.0.1\n")
    inv = inventory.inventory()
    assert adopting.pick_ip(100, inv[100], "30", None, {}, inv) == ("10.30.0.20", "ipconfig0")
    assert adopting.pick_ip(100, inv[100], "30", "10.30.0.20", {}, inv) == ("10.30.0.20", "ipconfig0")
    with pytest.raises(LabError, match=r"^--ip 10.30.0.21, but wg's ipconfig0 gives 10.30.0.20: leave --ip out"):
        adopting.pick_ip(100, inv[100], "30", "10.30.0.21", {}, inv)


def test_pick_ip_refuses_outside_range_and_other_gateway():
    hand_made(extra="ipconfig0: ip=192.168.1.20/24,gw=192.168.1.1\n")
    inv = inventory.inventory()
    with pytest.raises(LabError) as e:
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)
    assert str(e.value) == ("wg's ipconfig0 gives 192.168.1.20/24, outside prod (10.30.0.0/24): give it an address of "
                            "prod (qm set 100 --ipconfig0 ip=ADDR/24,gw=10.30.0.1; cloud-init sets it at the next "
                            "boot), then run pvelab adopt again")
    hand_made(extra="ipconfig0: ip=10.30.0.20/24,gw=10.30.0.254\n")
    inv = inventory.inventory()
    with pytest.raises(LabError) as e:
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)
    assert str(e.value) == ("wg's ipconfig0 has the gateway 10.30.0.254, prod's is 10.30.0.1: qm set 100 --ipconfig0 "
                            "ip=10.30.0.20/24,gw=10.30.0.1 (cloud-init sets it at the next boot), then run pvelab "
                            "adopt again")
    hand_made(extra="ipconfig0: ip=10.30.0.20/24\n")
    inv = inventory.inventory()
    with pytest.raises(LabError, match="has the gateway none, prod's is 10.30.0.1: qm set 100 --ipconfig0 "
                                       "ip=10.30.0.20/24,gw=10.30.0.1"):
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)
    hand_made(extra="ipconfig0: ip=10.30.0.20/16,gw=10.30.0.1\n")
    inv = inventory.inventory()
    with pytest.raises(LabError, match="qm set 100 --ipconfig0 ip=10.30.0.20/24,gw=10.30.0.1"):
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)


def test_pick_ip_needs_ip_without_a_fixed_address(monkeypatch):
    hand_made(extra="ipconfig0: ip=dhcp\n")
    inv = inventory.inventory()
    agent(monkeypatch, None)
    with pytest.raises(LabError) as e:
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)
    assert str(e.value) == ("wg has no fixed address in its config (ipconfig0: ip=dhcp): pvelab needs the address it "
                            "has on prod - give it with --ip ADDR (recorded only; nothing in the VM changes)")
    agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.20", "172.17.0.1")])
    with pytest.raises(LabError, match=r"--ip ADDR \(recorded only; nothing in the VM changes\); its guest agent "
                                       r"reports 10.30.0.20$"):
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)
    assert adopting.pick_ip(100, inv[100], "30", "10.30.0.20", {}, inv) == ("10.30.0.20", "--ip")
    hand_made()
    inv = inventory.inventory()
    agent(monkeypatch, None)
    with pytest.raises(LabError, match=r"\(ipconfig0: none\)"):
        adopting.pick_ip(100, inv[100], "30", None, {}, inv)
    from pvelab.helptext import UsageError
    with pytest.raises(UsageError, match=r"^--ip 10.31.0.5: an address of prod \(10.30.0.0/24\)$"):
        adopting.pick_ip(100, inv[100], "30", "10.31.0.5", {}, inv)
    with pytest.raises(UsageError, match=r"^--ip x: an address of prod"):
        adopting.pick_ip(100, inv[100], "30", "x", {}, inv)


def test_pick_ip_refuses_gateway_and_clashes():
    hand_made()
    inv = inventory.inventory()
    with pytest.raises(LabError, match=r"^10.30.0.1 is prod's gateway: "):
        adopting.pick_ip(100, inv[100], "30", "10.30.0.1", {}, inv)
    with pytest.raises(LabError, match=r"^10.30.0.255 is no host address of prod \(10.30.0.0/24\)"):
        adopting.pick_ip(100, inv[100], "30", "10.30.0.255", {}, inv)
    rows = {201: {"name": "prod-alice", "net": "30", "ip": "10.30.0.11"}, 301: {"name": "x", "net": "31", "ip": "10.30.0.12"}}
    with pytest.raises(LabError) as e:
        adopting.pick_ip(100, inv[100], "30", "10.30.0.11", rows, inv)
    assert str(e.value) == ("10.30.0.11 is prod-alice's address already: two VMs can't share it - change wg's "
                            "address first")
    assert adopting.pick_ip(100, inv[100], "30", "10.30.0.12", rows, inv) == ("10.30.0.12", "--ip")   # another net's
    hand_made(101, "web", extra="ipconfig0: ip=10.30.0.40/24,gw=10.30.0.1\n")
    hand_made(102, "far", bridge="vmbr31", extra="ipconfig0: ip=10.30.0.41/24,gw=10.30.0.1\n")
    inv = inventory.inventory()
    with pytest.raises(LabError, match="^10.30.0.40 is web's address already"):
        adopting.pick_ip(100, inv[100], "30", "10.30.0.40", {}, inv)
    assert adopting.pick_ip(100, inv[100], "30", "10.30.0.41", {}, inv) == ("10.30.0.41", "--ip")


def test_check_agent_refuses_a_different_address(monkeypatch):
    hand_made()
    agent(monkeypatch, [iface("bc:24:11:aa:bb:cc", "10.30.0.21")])
    with pytest.raises(LabError) as e:
        adopting.check_agent(100, conf_of(), "10.30.0.20", "ipconfig0", "a", True)
    assert str(e.value) == ("wg's guest agent reports 10.30.0.21 on net0, not 10.30.0.20: pvelab's rules would follow "
                            "an address it does not have. Fix its address (or --ip), then run pvelab adopt again")
    assert adopting.check_agent(100, conf_of(), "10.30.0.21", "--ip", "a", True) is None


def test_check_agent_warns_only_for_ip_given(monkeypatch):
    hand_made()
    asked = agent(monkeypatch, None)
    assert adopting.check_agent(100, conf_of(), "10.30.0.20", "--ip", "a", False) == (
        "10.30.0.20 not checked (wg is not running): pvelab's rules follow the address recorded")
    assert adopting.check_agent(100, conf_of(), "10.30.0.20", "ipconfig0", "a", False) is None
    assert asked == []                                   # a stopped VM's agent is not asked
    assert adopting.check_agent(100, conf_of(), "10.30.0.20", "--ip", "a", True) == (
        "10.30.0.20 not checked (its guest agent did not answer): pvelab's rules follow the address recorded")
    assert adopting.check_agent(100, conf_of(), "10.30.0.20", "ipconfig0", "a", True) is None


def test_shared_needs_the_fixed_mac_on_an_sdn_net(monkeypatch):
    monkeypatch.setattr(adopting.config, "is_sdn", lambda n: True)
    hand_made(bridge="lab30")
    inv = inventory.inventory()
    why = adopting.shared_problem(100, inv[100], "30", inv)
    assert why == ("wg's net0 has the MAC bc:24:11:aa:bb:cc; prod's shared VM needs the fixed MAC 02:00:0a:0a:1e:02 "
                   "(VMs on other hosts reach it by that). Shut it down, then: qm set 100 --net0 "
                   "virtio=02:00:0a:0a:1e:02,bridge=lab30,firewall=1; start it and run pvelab adopt again (a guest "
                   "whose network config names the old MAC needs that changed too)")
    hand_made(bridge="lab30", mac="02:00:0A:0A:1E:02")
    inv = inventory.inventory()
    assert adopting.shared_problem(100, inv[100], "30", inv) is None


def test_shared_mac_not_needed_on_a_bridge_net():
    hand_made()
    inv = inventory.inventory()
    assert adopting.shared_problem(100, inv[100], "30", inv) is None


def test_shared_refused_when_the_net_has_one():
    hand_made()
    record(210, "box", role="shared")
    inv = inventory.inventory()
    assert adopting.shared_problem(100, inv[100], "30", inv).startswith(
        "box is prod's shared VM already (one per net: they share a fixed MAC)")


def test_owner_key_drops_the_net_name_and_checks_it():
    hand_made()
    from pvelab.helptext import UsageError
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
    assert adopting.login({"name": "wg", "ciuser": "debian"}, None) == "debian"
    assert adopting.login({"name": "wg", "ciuser": "debian"}, "debian") == "debian"
    with pytest.raises(LabError) as e:
        adopting.login({"name": "wg", "ciuser": "debian"}, "root")
    assert str(e.value) == ("wg's cloud-init user is debian: pvelab logs in as that (passwd, handout) - leave --user "
                            "out or give --user debian")


def test_login_needs_user_without_ciuser():
    from pvelab.helptext import UsageError
    assert adopting.login({"name": "wg"}, "admin") == "admin"
    with pytest.raises(UsageError, match="^--user: 'Admin' is not a login name"):
        adopting.login({"name": "wg"}, "Admin")
    with pytest.raises(LabError) as e:
        adopting.login({"name": "wg"}, None)
    assert str(e.value) == "wg has no cloud-init user (ciuser): --user USER names the login pvelab hands out"


# --- Task 3: pvelab adopt ---

GOOD = "ciuser: debian\nipconfig0: ip=10.30.0.20/24,gw=10.30.0.1\nide2: local-lvm:vm-100-cloudinit,media=cdrom\n"


@pytest.fixture
def cmd(monkeypatch):
    """The adopt module with its host side faked: quorum, qm status (stopped), the net's rules (refreshed: the
    nets in cmd.refreshed), the guest agent (no answer)."""
    from pvelab.commands import adopt
    adopt.refreshed = []
    monkeypatch.setattr(adopt, "require_quorum", lambda doing: None)
    monkeypatch.setattr(adopt, "vm_status", lambda vmid: "stopped")
    monkeypatch.setattr(adopt, "refresh_net", lambda n: adopt.refreshed.append(n))
    monkeypatch.setattr(adopt, "ensure_gw_mac", lambda n: adopt.refreshed.append(f"gw {n}"))
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
    path = __import__("pathlib").Path(inventory.conf_path(100))
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
    assert not __import__("os").path.exists(registry.REGISTRY) and cmd.refreshed == []


def test_adopted_vm_counts_everywhere(cmd):
    from pvelab import credentials, isolation, owners
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
    monkeypatch.setattr(adopt, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(adopt, "gateway_elsewhere", lambda n: "b")
    hand_made(extra=GOOD)


REFUSALS = {
    "template": (lambda mp, a: hand_made(extra=GOOD + "template: 1\n"), "wg (100) is a template: pvelab adopt takes VMs"),
    "record": (lambda mp, a: record(100, "wg"), "wg (100) is pvelab's already (pvelab list shows it)"),
    "legacy": (lambda mp, a: legacy_vm(), "wg (100) was made by an older pvelab: pvelab migrate records it"),
    "no uuid": (lambda mp, a: vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr30\n" + GOOD),
                "wg (100) has no smbios1 uuid, which pvelab's record is matched by: qm set 100 --smbios1 "
                "uuid=$(cat /proc/sys/kernel/random/uuid), then run pvelab adopt again"),
    "no name": (lambda mp, a: vm(100, f"net0: virtio={MAC},bridge=vmbr30\nsmbios1: uuid=1\n" + GOOD),
                "VM 100 has no name: qm set 100 --name NAME, then run pvelab adopt again"),
    "net2 as name": (lambda mp, a: hand_made(name="net2", extra=GOOD),
                     "net2: 'net2' means something else to pvelab (all VMs, a net, a bridge) - qm set 100 --name NEW, "
                     "then run pvelab adopt again"),
    "pvelab's name": (lambda mp, a: (record(201, "wg"), hand_made(extra=GOOD)),
                      "VM name wg is pvelab's VM 201 already: give one of them another name first (pvelab rename, or "
                      "qm set 100 --name NEW)"),
    "vmbr0": (lambda mp, a: hand_made(bridge="vmbr0", extra=GOOD),
              "wg's net0 is on vmbr0, not on one of pvelab's nets (prod, other): pvelab adopt takes a VM whose net0 "
              "is on its net"),
    "gateway elsewhere": (gateway_elsewhere, "prod's gateway is on b: its rules (isolation, ssh forwards, blocks) are "
                                             "there - run pvelab adopt on that host"),
    "no quorum": (no_quorum, "the cluster has no quorum (b is offline), so PVE refuses changes: no VM can be adopted"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_adopt_refusals(cmd, monkeypatch, case):
    setup, text = REFUSALS[case]
    setup(monkeypatch, cmd)
    had = registry.load()
    with pytest.raises(LabError) as e:
        cmd.cmd_adopt(["100", "--owner", "alice"])
    assert str(e.value) == text
    assert registry.load() == had and cmd.refreshed == []


def test_adopt_exposed_net_warns_about_ssh_from_outside(cmd, monkeypatch, capsys):
    from pvelab import ui
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
    from pvelab import ui
    monkeypatch.setattr(ui, "YES", True)
    vm(100, f"name: wg\nnet0: virtio={MAC},bridge=vmbr30\nnet1: virtio=BC:24:11:00:00:09,bridge=vmbr0\n"
            "smbios1: uuid=0000-100\nipconfig0: ip=dhcp\n")
    cmd.cmd_adopt(["wg", "--owner", "alice", "--user", "admin", "--ip", "10.30.0.30"])
    out = capsys.readouterr().out
    assert "address 10.30.0.30 (as given), login admin, password none recorded (pvelab passwd wg sets one)" in out
    assert "! 10.30.0.30 not checked (wg is not running): pvelab's rules follow the address recorded" in out
    assert "! wg has no cloud-init drive: pvelab rename and net set change its config only" in out
    assert "! wg also has net1 on vmbr0: pvelab's rules cover net0 only" in out
    assert rec(100)["ip"] == "10.30.0.30" and rec(100)["user"] == "admin"


def test_adopt_owner_with_vms_says_so(cmd, capsys):
    record(201, "prod-alice", owner="alice")
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--owner", "alice"])
    assert "owner alice's VM (with their 1 VM(s) on prod)" in capsys.readouterr().out


def test_adopt_rules_failure_keeps_the_record(cmd, monkeypatch, capsys):
    def fail(n):
        raise LabError("iptables: no chain")
    monkeypatch.setattr(cmd, "refresh_net", fail)
    hand_made(extra=GOOD)
    cmd.cmd_adopt(["wg", "--shared"])
    assert rec(100)["role"] == "shared"
    assert ("wg is recorded; prod's rules not brought up to date (iptables: no chain) - pvelab isolate --net prod"
            in capsys.readouterr().err)


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
    from pvelab.helptext import UsageError
    with pytest.raises(UsageError) as e:
        cmd.cmd_adopt(argv)
    assert str(e.value) == text


def test_handout_card_takes_the_recorded_password(tmp_path):
    from pvelab.commands import handout
    record(100, "wg", owner="alice", password="")
    record(201, "prod-alice", ip="10.30.0.12", owner="alice")
    out = tmp_path / "cards.html"
    handout.cmd_handout(["--net", "prod", "-o", str(out)])
    assert "Password: <code>pw</code>" in out.read_text()
    record(100, "wg", owner="alice", password="")
    registry.update({201: None}, {})
    handout.cmd_handout(["--net", "prod", "-o", str(out)])
    assert "Password: <code>-</code>" in out.read_text()


def test_list_template_column_dash_when_none_recorded(monkeypatch, capsys):
    listing = fake_list(monkeypatch)
    record(201, "prod-alice")
    listing.cmd_list([])
    row = capsys.readouterr().out.splitlines()[1]
    assert row.split()[:3] == ["prod-alice", "201", "-"]
