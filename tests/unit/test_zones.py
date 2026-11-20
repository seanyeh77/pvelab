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
