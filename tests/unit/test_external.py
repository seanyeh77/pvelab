"""External networks: a bridge that exists in PVE but is not pvelab's (vmbr0, a vmbr1 made by hand).
VMs get their address from it (DHCP); pvelab keeps owners, passwords and snapshots, no rules of its own."""
import pytest

from pvelab import config, nets
from pvelab.util import LabError


def test_kinds_of_net_ids():
    assert config.is_external("vmbr0") and config.is_external("br-lan")
    assert not config.is_external("2") and not config.is_external("dhcp") and not config.is_external("")
    assert config.is_plain("dhcp") and config.is_plain("vmbr0") and not config.is_plain("2")


def test_an_external_net_is_its_bridge():
    assert config.bridge("vmbr0") == "vmbr0" and nets.net_name("vmbr0") == "vmbr0"
    assert not config.is_sdn("vmbr0") and not config.taken_over("vmbr0") and not config.in_sdn("vmbr0")


def test_resolve_an_existing_bridge_that_is_not_pvelabs(monkeypatch):
    monkeypatch.setattr(nets, "all_nets", lambda: {"2": ("net2", None)})
    monkeypatch.setattr(nets, "host_bridge", lambda v: v in ("vmbr0", "vmbr1", "lab2"))
    monkeypatch.setattr(nets, "dhcp_bridge_ours", lambda: True)
    monkeypatch.setattr(nets.config, "bridge", lambda n: {"2": "lab2"}.get(str(n), f"x{n}"))
    assert nets.resolve("vmbr0") == "vmbr0"
    assert nets.resolve("vmbr1") == "dhcp"                  # pvelab's own DHCP bridge
    assert nets.resolve("lab2") == "2"                      # a pvelab net, by its bridge
    assert nets.resolve("vmbr9") is None                    # no such bridge here
    monkeypatch.setattr(nets, "dhcp_bridge_ours", lambda: False)
    assert nets.resolve("vmbr1") == "vmbr1"                 # a vmbr1 made by hand: external


def test_sorted_nets_with_external_ones():
    from pvelab.inventory import sorted_nets
    assert sorted_nets({"10", "2", "vmbr0", "dhcp", ""}) == ["2", "10", "vmbr0"]


def test_dhcp_bridge_ours_by_its_address(monkeypatch, tmp_path):
    f = tmp_path / "interfaces"
    monkeypatch.setattr(nets.config, "IFACES", str(f))
    monkeypatch.setattr(nets, "host_bridge", lambda v: True)
    f.write_text("auto vmbr1\niface vmbr1 inet static\n        address 10.10.10.1/24\n")
    assert nets.dhcp_bridge_ours()
    f.write_text("auto vmbr1\niface vmbr1 inet static\n        address 192.168.50.1/24\n")
    assert not nets.dhcp_bridge_ours()
    f.write_text("auto vmbr0\niface vmbr0 inet static\n")
    monkeypatch.setattr(nets, "host_bridge", lambda v: False)
    assert nets.dhcp_bridge_ours()                          # none yet: pvelab makes it


def test_isolate_and_expose_refuse_an_external_net(monkeypatch):
    from pvelab.commands import access
    monkeypatch.setattr(access, "net_arg", lambda v: "vmbr0")
    with pytest.raises(LabError, match="vmbr0 is not pvelab's network"):
        access.net_switch_args(["--net", "vmbr0"])


def test_pvelab_does_not_take_a_vmbr1_made_by_hand(monkeypatch):
    from pvelab import networks
    monkeypatch.setattr(networks.nets, "dhcp_bridge_ours", lambda: False)
    monkeypatch.setattr(networks, "link_exists", lambda d: pytest.fail("touched it"))
    with pytest.raises(LabError, match="vmbr1 is not pvelab's"):
        networks.ensure_dhcp_bridge()


def test_create_plan_warns_for_an_external_net(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.netcheck, "on_openstack", lambda: True)
    lines = create.external_warnings("vmbr0")
    assert any("harden" in w for w in lines) and any("first boot" in w for w in lines)
    assert any("OpenStack" in w for w in lines)
    assert create.external_warnings("vmbr0", checked=True) == lines[:1]       # tried: it gets a network


def test_create_on_an_external_net(monkeypatch):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    monkeypatch.setattr(create.nets, "resolve", lambda v: "vmbr0" if v == "vmbr0" else None)
    monkeypatch.setattr(create.netcheck, "on_openstack", lambda: False)
    monkeypatch.setattr(create.netcheck, "check", lambda br, mirror: [("DHCP", True, "192.168.1.5/24"),
                                                                     ("packages", True, "200")])
    monkeypatch.setattr(create, "foreign_use", lambda n: pytest.fail("checked an admin's bridge"))
    monkeypatch.setattr(create, "next_ip", lambda n, rows: pytest.fail("gave a fixed address"))
    create.cmd_create(["2x2", "debian13", "--net", "vmbr0"])
    assert [g[0] for g in got] == ["vmbr0-student01-1", "vmbr0-student01-2", "vmbr0-student02-1", "vmbr0-student02-2"]
    with pytest.raises(LabError, match="not pvelab's network"):
        create.cmd_create(["1", "debian13", "--net", "vmbr0", "--expose"])


def test_list_says_an_external_net_is_not_pvelabs(monkeypatch, capsys):
    from pvelab.commands import listing
    rows = {301: {"name": "vmbr0-user01", "net": "vmbr0", "user": "user", "password": "pw", "ip": "-"}}
    monkeypatch.setattr(listing, "vm_ip", lambda i, rows: "192.168.1.50")
    listing.list_ssh({301: {"name": "vmbr0-user01"}}, rows, {301: "vmbr0"}, set(), set())
    out = capsys.readouterr().out
    assert "ssh user@192.168.1.50" in out and "VMs on vmbr0: ssh to their own addresses" in out
    assert "pvelab expose" not in out


def test_net_commands_refuse_an_external_net(monkeypatch):
    from pvelab import readdress
    monkeypatch.setattr(readdress, "net_arg", lambda v: "vmbr0")
    with pytest.raises(LabError, match="vmbr0 is not pvelab's network: nothing of it to rename"):
        readdress.local_net("vmbr0", "rename")


def test_owner_and_migrate_take_vms_of_an_external_net(monkeypatch):
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    assert nets.known_here("vmbr0", {"2"})
    conf = {"name": "vmbr0-user01", "description": "managed-by=lab.sh net=vmbr0", "tags": "lab-student"}
    todo, problems, far = migrate.plan({301: conf})
    assert far == [] and todo[301][0].endswith("net=vmbr0 owner=user01") and "net-vmbr0" in todo[301][1]


def test_the_menu_offers_no_shared_vm_on_an_external_net(monkeypatch):
    import importlib
    test_ux = importlib.import_module("test_ux")
    from pvelab import wizard
    monkeypatch.setattr(wizard.Wizard, "net_key", staticmethod(lambda a: "vmbr0" if a == "vmbr0" else "2"))
    data = {**test_ux.DATA, "nets": ["vmbr0"], "shared": []}
    with pytest.raises(wizard.Back):
        wizard.Wizard(data).shared()                         # no net of pvelab's: nothing to offer


def test_a_pve_vnet_is_not_an_external_net(monkeypatch):
    monkeypatch.setattr(nets, "all_nets", lambda: {})
    monkeypatch.setattr(nets.config, "taken_vnets", lambda: {})
    monkeypatch.setattr(nets, "host_bridge", lambda v: True)              # an applied vnet is a bridge too
    monkeypatch.setattr(nets, "sdn_vnets", lambda: {"vnet1": ("z1", None)})
    monkeypatch.setattr(nets, "pve_vnet", lambda v: {"type": "simple", "cidr": "10.9.0.0/24", "gateway": "10.9.0.1"})
    monkeypatch.setattr(nets, "dhcp_bridge_ours", lambda: True)
    assert nets.resolve("vnet1") is None                    # free_vnet takes it over
    assert nets.resolve("vmbr0") == "vmbr0"


def test_create_on_vmbr1_is_checked_as_no_net(monkeypatch):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    monkeypatch.setattr(create.nets, "resolve", lambda v: "dhcp" if v == "vmbr1" else None)
    with pytest.raises(LabError, match="--shared needs --net"):
        create.cmd_create(["debian13", "--net", "vmbr1", "--shared"])
    with pytest.raises(LabError, match="NxM"):
        create.cmd_create(["2x2", "debian13", "--net", "vmbr1"])
    assert got == []


def test_handout_for_an_external_vm_without_an_address(monkeypatch, tmp_path):
    from pvelab.commands import handout
    monkeypatch.setattr(handout, "targets", lambda a: [301])
    monkeypatch.setattr(handout, "cred_rows", lambda: {301: {"net": "vmbr0", "user": "user", "password": "pw"}})
    monkeypatch.setattr(handout, "forwarded_on", lambda nets: set())
    monkeypatch.setattr(handout, "vm_names", lambda ids, confs=None: {i: "vmbr0-user01" for i in ids})
    monkeypatch.setattr(handout, "vm_ip", lambda i, rows: "-")
    out = tmp_path / "h.html"
    handout.cmd_handout(["301", "-o", str(out)])
    page = out.read_text()
    assert "user@-" not in page and "no address yet" in page and "not open yet" in page


def test_block_refuses_a_vm_on_an_external_net(monkeypatch):
    from pvelab.commands import access
    monkeypatch.setattr(access, "targets", lambda a: [301])
    monkeypatch.setattr(access, "cred_rows", lambda: {301: {"net": "vmbr0"}})
    monkeypatch.setattr(access, "vm_names", lambda ids: {301: "vmbr0-user01"})
    monkeypatch.setattr(access, "blocks_save", lambda b: pytest.fail("saved a block"))
    with pytest.raises(LabError, match="vmbr0"):
        access.cmd_block(["internet", "vmbr0-user01"])


def test_the_menu_and_net_create_refuse_an_existing_bridge(monkeypatch):
    from pvelab import wizard
    from pvelab.commands import netcmd
    monkeypatch.setattr(nets, "resolve", lambda v: "vmbr0" if v == "vmbr0" else None)
    monkeypatch.setattr(nets, "free_vnet", lambda v: None)
    assert "--net vmbr0" in wizard.check_net("vmbr0")
    with pytest.raises(LabError, match="bridge of this host"):
        netcmd.cmd_net_create(["create", "vmbr0"])


def test_net_check_on_a_pvelab_net_uses_its_fixed_address(monkeypatch):
    from pvelab.commands import netcmd
    monkeypatch.setattr(nets, "resolve", lambda v: "30")
    monkeypatch.setattr(nets, "host_bridge", lambda v: True)
    monkeypatch.setattr(nets, "cidr", lambda n: __import__("ipaddress").ip_network("10.10.30.0/24"))
    monkeypatch.setattr(nets, "gateway", lambda n: __import__("ipaddress").ip_address("10.10.30.1"))
    monkeypatch.setattr(netcmd, "host_dns", lambda: "10.10.30.1")
    got = {}
    monkeypatch.setattr(netcmd.netcheck, "check", lambda br, mirror, static=None: got.update(br=br, static=static)
                        or [("address", True, "x"), ("packages", True, "200")])
    monkeypatch.setattr(nets, "gateway", lambda n: __import__("ipaddress").ip_address("10.10.30.254"))
    monkeypatch.setattr(netcmd, "cred_rows", lambda: {201: {"net": "30", "ip": "10.10.30.253"}})
    monkeypatch.setattr(netcmd, "ok", lambda *c: c[-1] == "10.10.30.252")      # answers: someone has it
    netcmd.cmd_net_check(["check", "net30"])
    assert got == {"br": config.bridge("30"), "static": ("10.10.30.251", 24, "10.10.30.254", ["10.10.30.1"])}


def test_a_pve_vnet_pvelab_cannot_take_over_is_external(monkeypatch):
    monkeypatch.setattr(nets, "all_nets", lambda: {})
    monkeypatch.setattr(nets.config, "taken_vnets", lambda: {})
    monkeypatch.setattr(nets, "host_bridge", lambda v: True)
    monkeypatch.setattr(nets, "sdn_vnets", lambda: {"vlan20": ("z2", None), "vnet1": ("z1", None)})
    monkeypatch.setattr(nets, "pve_vnet", lambda v: {"vlan20": {"type": "vlan", "cidr": None, "gateway": None},
                                                     "vnet1": {"type": "simple", "cidr": "10.9.0.0/24",
                                                               "gateway": "10.9.0.1"}}[v])
    assert nets.resolve("vlan20") == "vlan20"               # a VLAN of its own: VMs there use its DHCP
    assert nets.resolve("vnet1") is None                    # free_vnet takes it over


def test_net_check_on_a_free_pve_vnet(monkeypatch):
    from pvelab.commands import netcmd
    monkeypatch.setattr(nets, "resolve", lambda v: None)
    monkeypatch.setattr(nets, "host_bridge", lambda v: v == "vnet1")
    assert netcmd.check_bridge("vnet1") == ("vnet1", None)


def test_vmbr1_is_named_pvelabs_dhcp_bridge(monkeypatch):
    from pvelab import wizard
    from pvelab.commands import netcmd
    monkeypatch.setattr(nets, "resolve", lambda v: "dhcp")
    assert "pvelab's DHCP bridge" in wizard.check_net("vmbr1")
    with pytest.raises(LabError, match="pvelab's DHCP bridge"):
        netcmd.cmd_net_create(["create", "vmbr1"])
