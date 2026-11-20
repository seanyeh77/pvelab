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
