"""pvelab adopt: a VM made by hand (PVE's web UI) on one of pvelab's nets becomes pvelab's; pvelab list names
such VMs. No host commands: qm and the guest agent are faked."""
import pytest

from pvelab import adopting, config, inventory, networks, nodes, registry

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
