import pytest

from pvelab import config, zones
from pvelab.util import LabError


def write_zones(text):
    with open(config.SDN_ZONES, "w") as f:
        f.write(text)


def test_valid_id_follows_pves_rule():
    assert zones.valid_id("SDC") == "SDC" and zones.valid_id("t97") == "t97"
    for bad in ("S", "toolongname", "9abc", "a-b", "ab_"):
        with pytest.raises(LabError, match="2 to 8 letters or digits"):
            zones.valid_id(bad)


def test_zones_reads_type_nodes_peers_mtu():
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1, 10.0.0.2\n\tnodes a,b\n\tmtu 1450\n\nsimple: S1\n\tipam pve\n")
    assert zones.zones() == {"SDC": {"type": "vxlan", "nodes": ["a", "b"], "peers": ["10.0.0.1", "10.0.0.2"],
                                     "mtu": 1450},
                             "S1": {"type": "simple", "nodes": None, "peers": None, "mtu": None}}


def test_no_zones_file_is_no_zones():
    assert zones.zones() == {}


def test_hosts_are_the_zone_nodes_or_none():
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1\n\tnodes a\n\nvxlan: ALL\n\tpeers 10.0.0.1\n")
    assert zones.hosts("SDC") == ["a"] and zones.hosts("ALL") is None


def test_auto_peers_are_the_zone_hosts_or_every_host():
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1\n\tnodes a\n\nvxlan: ALL\n\tpeers 10.0.0.1\n")
    addrs = {"a": "10.0.0.1", "b": "10.0.0.2"}
    assert zones.auto_peers("SDC", addrs) == ["10.0.0.1"]
    assert zones.auto_peers("ALL", addrs) == ["10.0.0.1", "10.0.0.2"]


def test_peers_compare_as_sets():
    write_zones("vxlan: SDC\n\tpeers 10.0.0.2, 10.0.0.1\n")
    assert not zones.hand_tuned("SDC", {"a": "10.0.0.1", "b": "10.0.0.2"})
    assert zones.hand_tuned("SDC", {"a": "10.0.0.1"})


def test_only_vxlan_zones_are_hand_tuned():
    write_zones("simple: S1\n\tipam pve\n")
    assert not zones.hand_tuned("S1", {"a": "10.0.0.1"})


def test_drift_line_says_what_and_how_to_align():
    write_zones("vxlan: SDC\n\tpeers 10.0.0.9\n")
    assert zones.drift_line("SDC", {"a": "10.0.0.1", "b": "10.0.0.2"}) == (
        "SDC: peers 10.0.0.9 differ from its hosts' addresses (10.0.0.1, 10.0.0.2): left as they are "
        "(pvelab zone set SDC --peers auto aligns them)")


def test_addresses_without_a_cluster_is_this_host_uplink():
    from pvelab import nodes
    assert zones.addresses() == {nodes.this_node(): "192.0.2.1"}


def test_peer_plan_follows_auto_zones_and_leaves_hand_tuned():
    write_zones("vxlan: ALL\n\tpeers 10.0.0.1\n\nvxlan: SDC\n\tpeers 10.0.0.1\n\tnodes a\n\n"
                "vxlan: HAND\n\tpeers 10.9.9.9\n\nsimple: S1\n\tipam pve\n")
    changes, drift = zones.peer_plan({"a": "10.0.0.1"}, {"a": "10.0.0.1", "b": "10.0.0.2"})
    assert changes == {"ALL": ["10.0.0.1", "10.0.0.2"]}
    assert len(drift) == 1 and drift[0].startswith("HAND: peers 10.9.9.9")


def test_peer_plan_leaves_peers_already_set_for_the_hosts_after():
    write_zones("vxlan: ALL\n\tpeers 10.0.0.1, 10.0.0.2\n")
    assert zones.peer_plan({"a": "10.0.0.1"}, {"a": "10.0.0.1", "b": "10.0.0.2"}) == ({}, [])


def test_single_host_default_zone_needs_no_peer_change():
    write_zones("vxlan: pvelab\n\tpeers 192.0.2.1\n\tmtu 1450\n")
    addrs = zones.addresses()
    assert zones.peer_plan(addrs, dict(addrs)) == ({}, [])


def test_net_zone_is_the_chosen_one_else_its_vnets_else_the_default():
    from pvelab import config
    assert zones.net_zone("5") == config.SDN_ZONE
    with open(config.SDN_VNETS, "w") as f:
        f.write("vnet: lab6\n\tzone SDC\n\ttag 4200006\n")
    assert zones.net_zone("6") == "SDC"
    zones.choose("5", "OTHER", ["a"])
    assert zones.net_zone("5") == "OTHER" and zones.chosen_nodes("5") == ["a"] and zones.chosen_nodes("6") is None


def test_offline_host_keeps_its_address_so_auto_zones_still_follow(monkeypatch):
    import json
    from pvelab import nodes
    with open(nodes.MEMBERS, "w") as f:
        f.write(json.dumps({"nodename": "a", "version": 4, "nodelist": {
            "a": {"id": 1, "online": 1, "ip": "10.0.0.1"}, "b": {"id": 2, "online": 1, "ip": "10.0.0.2"},
            "c": {"id": 3, "online": 0}, "d": {"id": 4, "online": 1, "ip": "10.0.0.4"}}}))
    with open(nodes.COROSYNC_CONF, "w") as f:
        f.write("nodelist {\n" + "".join(
            f"  node {{\n    name: {n}\n    nodeid: {i}\n    ring0_addr: 10.0.0.{i}\n  }}\n"
            for i, n in enumerate("abcd", 1)) + "}\n")
    write_zones("vxlan: ALL\n\tpeers 10.0.0.1, 10.0.0.2, 10.0.0.3\n")
    addrs = zones.addresses()
    assert addrs["c"] == "10.0.0.3"
    before = {h: ip for h, ip in addrs.items() if h != "d"}
    assert zones.peer_plan(before, addrs) == ({"ALL": ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]}, [])
    write_zones("vxlan: ALL\n\tpeers 10.0.0.1, 10.0.0.2, 10.0.0.3, 10.0.0.4\n")
    assert zones.peer_plan(addrs, before) == ({"ALL": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]}, [])


def test_net_hosts_of_a_zone_with_nodes(monkeypatch):
    from pvelab import placement
    monkeypatch.setattr(placement, "is_sdn", lambda n: True)
    write_zones("vxlan: SDC\n\tnodes a,b\n\tpeers 10.0.0.1, 10.0.0.2\n")
    with open(config.SDN_VNETS, "w") as f:
        f.write("vnet: lab6\n\tzone SDC\n\ttag 4200006\n")
    assert zones.net_hosts("6") == ["a", "b"]


def test_net_hosts_of_a_zone_on_every_host_is_none(monkeypatch):
    from pvelab import placement
    monkeypatch.setattr(placement, "is_sdn", lambda n: True)
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1\n")
    with open(config.SDN_VNETS, "w") as f:
        f.write("vnet: lab6\n\tzone SDC\n\ttag 4200006\n")
    assert zones.net_hosts("6") is None
    assert zones.net_hosts("7") is None               # no vnet yet: the default zone, which has no nodes


def test_net_hosts_of_a_plain_bridge_is_here(monkeypatch):
    from pvelab import nodes, placement
    monkeypatch.setattr(placement, "is_sdn", lambda n: False)
    assert zones.net_hosts("30") == [nodes.this_node()]
    assert zones.net_hosts("dhcp") == [nodes.this_node()]


def taken_over_net5(kind):
    """Net 5: PVE vnet vnet1 pvelab took over, in a zone SDC of type kind on hosts a and b."""
    write_zones(f"{kind}: SDC\n\tnodes a,b\n")
    with open(config.SDN_VNETS, "w") as f:
        f.write("vnet: vnet1\n\tzone SDC\n")
    with open(config.IFACES, "a") as f:
        f.write("\n# lab.sh net 5 begin (managed by lab)\n        # pvelab name prod\niface vnet1 inet manual\n"
                "# lab.sh net 5 end\n")


def test_net_hosts_of_a_taken_over_vnet_are_its_zone_hosts():
    taken_over_net5("evpn")
    from pvelab import placement
    assert config.taken_over("5") and zones.net_hosts("5") == ["a", "b"]
    assert placement.bridge_host("5") is None                  # move: not a one-host bridge


def test_net_hosts_of_a_taken_over_vnet_in_a_simple_zone_is_here():
    from pvelab import nodes
    taken_over_net5("simple")                         # a simple zone's vnet is not joined across its hosts
    assert zones.net_hosts("5") == [nodes.this_node()]


# --- net create --zone / --nodes, create --zone, and the zone commands ---

def three_hosts(c_online=True):
    """A cluster of a (this host), b and c, at 10.0.0.1-3."""
    import json
    from pvelab import nodes
    with open(nodes.MEMBERS, "w") as f:
        f.write(json.dumps({"nodename": "a", "version": 4, "nodelist": {
            "a": {"id": 1, "online": 1, "ip": "10.0.0.1"}, "b": {"id": 2, "online": 1, "ip": "10.0.0.2"},
            "c": {"id": 3, "online": int(c_online), **({"ip": "10.0.0.3"} if c_online else {})}}}))
    with open(nodes.COROSYNC_CONF, "w") as f:
        f.write("nodelist {\n" + "".join(f"  node {{\n    name: {n}\n    nodeid: {i}\n    ring0_addr: 10.0.0.{i}\n  }}\n"
                                          for i, n in enumerate("abc", 1)) + "}\n")


def write_vnets(text):
    with open(config.SDN_VNETS, "w") as f:
        f.write(text)


def net_create(monkeypatch, *args, here="a"):
    """pvelab net create prod ARGS on host here, everything but the zone checks faked: {zone, nodes} the
    net was made with."""
    from pvelab.commands import create, netcmd
    made = {}
    monkeypatch.setattr(netcmd, "this_node", lambda: here)
    monkeypatch.setattr(create, "this_node", lambda: here)
    monkeypatch.setattr(netcmd, "require_quorum", lambda doing: None)
    monkeypatch.setattr(netcmd, "pick_net", lambda v, c, g: "5")
    monkeypatch.setattr(netcmd, "foreign_use", lambda n: None)
    monkeypatch.setattr(netcmd, "is_sdn", lambda n: True)
    monkeypatch.setattr(create, "is_sdn", lambda n: True)
    monkeypatch.setattr(netcmd, "bridge", lambda n: "lab5")
    monkeypatch.setattr(netcmd.sdn, "refuse_foreign", lambda: None)
    monkeypatch.setattr(netcmd, "prepare_net", lambda n, iso, used: made.update(
        zone=zones.net_zone(n), nodes=zones.chosen_nodes(n)))
    netcmd.cmd_net_create(["create", "prod", *args])
    return made


def test_net_create_in_a_new_zone_makes_it_and_prints_the_permission_path(monkeypatch, capsys):
    three_hosts()
    assert net_create(monkeypatch, "--zone", "SDC", "--nodes", "a,b") == {"zone": "SDC", "nodes": ["a", "b"]}
    out = capsys.readouterr().out
    assert "path /sdn/zones/SDC, role PVESDNUser" in out and "zone SDC" in out


def test_net_create_in_an_existing_vxlan_zone_uses_it(monkeypatch, capsys):
    three_hosts()
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1, 10.0.0.2, 10.0.0.3\n")
    assert net_create(monkeypatch, "--zone", "SDC") == {"zone": "SDC", "nodes": None}
    assert "PVESDNUser" not in capsys.readouterr().out


def test_net_create_in_a_hand_tuned_zone_warns(monkeypatch, capsys):
    three_hosts()
    write_zones("vxlan: SDC\n\tpeers 10.0.0.9\n")
    net_create(monkeypatch, "--zone", "SDC")
    assert "SDC: peers 10.0.0.9 differ" in capsys.readouterr().err


def test_net_create_in_an_existing_vxlan_zone_refuses_a_simple_zone(monkeypatch):
    three_hosts()
    write_zones("simple: S1\n\tipam pve\n")
    with pytest.raises(LabError, match="S1 is a simple zone: pvelab makes nets in VXLAN zones only"):
        net_create(monkeypatch, "--zone", "S1")


def test_net_create_in_an_existing_vxlan_zone_refuses_different_nodes(monkeypatch):
    three_hosts()
    write_zones("vxlan: SDC\n\tnodes a,b\n\tpeers 10.0.0.1, 10.0.0.2\n")
    with pytest.raises(LabError, match=r"SDC exists already on a, b: pvelab zone set SDC --nodes \.\.\. changes that"):
        net_create(monkeypatch, "--zone", "SDC", "--nodes", "a")
    assert net_create(monkeypatch, "--zone", "SDC", "--nodes", "b,a")["zone"] == "SDC"     # the same hosts


def test_net_create_in_an_existing_vxlan_zone_refuses_when_this_host_is_outside(monkeypatch):
    three_hosts()
    write_zones("vxlan: SDC\n\tnodes b,c\n\tpeers 10.0.0.2, 10.0.0.3\n")
    with pytest.raises(LabError, match=r"a is not one of zone SDC's hosts \(b, c\)"):
        net_create(monkeypatch, "--zone", "SDC")
    with pytest.raises(LabError, match=r"a is not one of zone NEW's hosts \(b\)"):
        net_create(monkeypatch, "--zone", "NEW", "--nodes", "b")


def test_net_create_refuses_an_unknown_host_and_a_bad_zone_name(monkeypatch):
    three_hosts()
    with pytest.raises(LabError, match=r"zz is no host of this cluster \(hosts: a, b, c\)"):
        net_create(monkeypatch, "--zone", "SDC", "--nodes", "a,zz")
    with pytest.raises(LabError, match="zone name a-b"):
        net_create(monkeypatch, "--zone", "a-b")


def test_create_zone_on_an_existing_net_is_refused(monkeypatch):
    from pvelab.commands import create
    write_vnets("vnet: lab2\n\tzone pvelab\n\talias net2\n")
    monkeypatch.setattr(create, "require_quorum", lambda doing: None)
    monkeypatch.setattr(create, "pick_net", lambda v, c, g: "2")
    monkeypatch.setattr(create, "is_new_net", lambda n: False)
    monkeypatch.setattr(create, "is_sdn", lambda n: True)
    monkeypatch.setattr(create, "in_sdn", lambda n: True)
    monkeypatch.setattr(create, "taken_over", lambda n: False)
    with pytest.raises(LabError, match="net2 is in zone pvelab already: --zone is for a new net"):
        create.create(["2", "--net", "net2", "--zone", "SDC"])


def test_create_new_net_in_a_zone_chooses_it(monkeypatch):
    from pvelab.commands import create
    three_hosts()
    monkeypatch.setattr(create, "require_quorum", lambda doing: None)
    monkeypatch.setattr(create, "pick_net", lambda v, c, g: "5")
    monkeypatch.setattr(create, "is_new_net", lambda n: True)
    monkeypatch.setattr(create, "is_sdn", lambda n: True)
    monkeypatch.setattr(create, "in_sdn", lambda n: True)
    monkeypatch.setattr(create, "taken_over", lambda n: False)
    monkeypatch.setattr(create, "this_node", lambda: "a")
    monkeypatch.setattr(create, "foreign_use", lambda n: "stop here")
    with pytest.raises(LabError, match="stop here"):
        create.create(["2", "--net", "prod", "--zone", "SDC", "--nodes", "a,b"])
    assert zones.net_zone("5") == "SDC" and zones.chosen_nodes("5") == ["a", "b"]


def zone_cmd(monkeypatch, *args, dry=False, gateways=""):
    """pvelab zone ARGS with pvesh faked: the SDN calls made."""
    from pvelab.commands import zonecmd
    from pvelab import sdn, ui
    calls = []
    monkeypatch.setattr(ui, "YES", True)

    class Session:
        def __enter__(self):
            calls.append("session")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(sdn, "session", Session)
    monkeypatch.setattr(sdn, "set_zone", lambda z, nodes=None, peers=None: calls.append(("set", z, nodes, peers)))
    monkeypatch.setattr(sdn, "delete_zone", lambda z: calls.append(("delete", z)))
    monkeypatch.setattr(sdn, "apply", lambda: calls.append("apply"))
    monkeypatch.setattr(zonecmd, "run_on", lambda h, *cmd, **kw: calls.append(("cat", h)) or gateways)
    fn = {"set": zonecmd.cmd_zone_set, "remove": zonecmd.cmd_zone_remove}.get(args[0] if args else None)
    if fn:
        fn(list(args), **({"dry": True} if dry else {}))
    else:
        zonecmd.cmd_zone(list(args))
    return calls


def test_zone_list_columns_and_drift(monkeypatch, capsys):
    three_hosts()
    write_zones("vxlan: SDC\n\tnodes a,b\n\tpeers 10.0.0.2, 10.0.0.1\n\nvxlan: pvelab\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n\n"
                "vxlan: HAND\n\tpeers 10.9.9.9\n\nsimple: S1\n\tipam pve\n")
    write_vnets("vnet: lab5\n\tzone SDC\n\ttag 4200005\n\talias prod\n\nvnet: vnet1\n\tzone S1\n\nvnet: lab2\n\tzone pvelab\n"
                "\talias pvelab net2\n")
    zone_cmd(monkeypatch)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["ZONE", "TYPE", "HOSTS", "PEERS", "NETS"]
    row = {line.split()[0]: line for line in lines[1:5]}
    assert row["SDC"].split() == ["SDC", "vxlan", "a,", "b", "auto", "prod"]
    assert row["pvelab"].split() == ["pvelab", "vxlan", "every", "host", "auto", "net2"]
    assert row["HAND"].split()[-3:] == ["by", "hand", "-"] and row["S1"].split()[-2:] == ["-", "vnet1"]
    assert "HAND: peers 10.9.9.9 differ from its hosts' addresses" in out


def test_zone_list_without_zones(monkeypatch, capsys):
    zone_cmd(monkeypatch, "list")
    assert capsys.readouterr().out.strip() == "No SDN zones yet. pvelab net create NAME --zone ZONE makes one."


def guest(node, vmid, name, bridge, empty_pve):
    d = empty_pve / "nodes" / node / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(f"name: {name}\nnet0: virtio=AA,bridge={bridge}\n")


def test_zone_set_nodes_refuses_a_host_with_vms_on_the_zone(monkeypatch, empty_pve):
    three_hosts()
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n")
    write_vnets("vnet: myvnet\n\tzone SDC\n\talias web\n")             # a hand-made vnet: its guests count too
    guest("c", 301, "web1", "myvnet", empty_pve)
    guest("a", 302, "web2", "myvnet", empty_pve)
    with pytest.raises(LabError, match=r"c has guests on SDC's vnets: web1\. .*pvelab move"):
        zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b")


def test_zone_set_nodes_refuses_a_host_with_a_gateway_there(monkeypatch):
    three_hosts()
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n")
    write_vnets("vnet: lab5\n\tzone SDC\n\ttag 4200005\n\talias prod\n")
    block = "# lab.sh net 5 begin (managed by lab)\niface lab5 inet static\n# lab.sh net 5 end\n"
    with pytest.raises(LabError, match=r"c has the gateway of prod"):
        zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b", gateways=block)


def test_zone_set_nodes_refuses_a_host_with_the_gateway_of_a_taken_over_vnet(monkeypatch):
    from pvelab import config
    three_hosts()
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n")
    write_vnets("vnet: vnet1\n\tzone SDC\n\ttag 77\n")                  # PVE's own vnet, taken over as net 5
    config.save_taken({"vnet1": {"net": "5", "host": "c"}})
    block = "# lab.sh net 5 begin (managed by lab)\niface vnet1 inet static\n# lab.sh net 5 end\n"
    with pytest.raises(LabError, match=r"c has the gateway of vnet1"):
        zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b", gateways=block)
    three_hosts(c_online=False)
    with pytest.raises(LabError, match="can't check c for gateways of SDC's nets: it is offline"):
        zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b")


def test_zone_set_nodes_refuses_a_host_that_is_offline(monkeypatch):
    three_hosts(c_online=False)
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n")
    write_vnets("vnet: lab5\n\tzone SDC\n\ttag 4200005\n\talias prod\n")
    with pytest.raises(LabError, match="can't check c for gateways of SDC's nets: it is offline"):
        zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b")


def test_zone_set_nodes_moves_auto_peers_only(monkeypatch, capsys):
    three_hosts()
    write_zones("vxlan: SDC\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n\nvxlan: HAND\n\tpeers 10.9.9.9\n")
    write_vnets("vnet: lab5\n\tzone SDC\n\ttag 4200005\n\talias prod\n")
    calls = zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b")
    assert calls == [("cat", "c"), "session", ("set", "SDC", ["a", "b"], ["10.0.0.1", "10.0.0.2"]), "apply"]
    calls = zone_cmd(monkeypatch, "set", "HAND", "--nodes", "a,b")
    assert calls == ["session", ("set", "HAND", ["a", "b"], None), "apply"]
    assert "HAND: peers 10.9.9.9 differ" in capsys.readouterr().out
    write_zones("vxlan: SDC\n\tnodes a\n\tpeers 10.0.0.1\n")
    assert zone_cmd(monkeypatch, "set", "SDC", "--nodes", "all")[1:] == [
        ("set", "SDC", [], ["10.0.0.1", "10.0.0.2", "10.0.0.3"]), "apply"]
    assert zone_cmd(monkeypatch, "set", "SDC", "--nodes", "a,b", dry=True) == []


def test_zone_set_peers_auto_aligns(monkeypatch, capsys):
    three_hosts()
    write_zones("vxlan: SDC\n\tnodes a,b\n\tpeers 10.9.9.9\n")
    assert zone_cmd(monkeypatch, "set", "SDC", "--peers", "auto") == [
        "session", ("set", "SDC", None, ["10.0.0.1", "10.0.0.2"]), "apply"]
    write_zones("vxlan: SDC\n\tnodes a,b\n\tpeers 10.0.0.2, 10.0.0.1\n")
    assert zone_cmd(monkeypatch, "set", "SDC", "--peers", "auto") == []
    assert "already" in capsys.readouterr().out


def test_zone_set_refuses_other_zones_and_bad_options(monkeypatch):
    from pvelab.helptext import UsageError
    three_hosts()
    write_zones("simple: S1\n\tipam pve\n")
    with pytest.raises(LabError, match="S1 is a simple zone"):
        zone_cmd(monkeypatch, "set", "S1", "--nodes", "a")
    with pytest.raises(LabError, match="no SDN zone NOPE"):
        zone_cmd(monkeypatch, "set", "NOPE", "--nodes", "a")
    for args in (["set", "S1"], ["set", "S1", "--peers", "10.0.0.1"], ["set"]):
        with pytest.raises(UsageError):
            zone_cmd(monkeypatch, *args)


def test_zone_remove_refuses_while_any_vnet_is_there(monkeypatch):
    write_zones("vxlan: SDC\n\tpeers 192.0.2.1\n")
    write_vnets("vnet: myvnet\n\tzone SDC\n\talias web\n")
    with pytest.raises(LabError, match=r"SDC still has vnets: myvnet. Remove them first \(pvelab net remove NAME, or in PVE\)"):
        zone_cmd(monkeypatch, "remove", "SDC")


def test_zone_remove_deletes_an_empty_zone(monkeypatch, capsys):
    write_zones("vxlan: SDC\n\tpeers 192.0.2.1\n")
    write_vnets("vnet: lab5\n\tzone pvelab\n")
    assert zone_cmd(monkeypatch, "remove", "SDC", dry=True) == []
    assert zone_cmd(monkeypatch, "remove", "SDC") == ["session", ("delete", "SDC"), "apply"]
    assert "SDC removed" in capsys.readouterr().out
