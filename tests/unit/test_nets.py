import ipaddress
import types

from pvelab import ifaces as ifaces_mod

import pytest

from pvelab import config, isolation, lifecycle, nets, networks, sdn
from pvelab.util import LabError

OLD = ("# lab.sh net 30 begin (managed by lab - removed automatically with its last VM)\n"
       "auto vmbr30\niface vmbr30 inet static\n        address 10.10.30.1/24\n# lab.sh net 30 end\n")
NEW = ("# lab.sh net 31 begin (managed by lab - removed automatically with its last VM)\n"
       "        # pvelab name docker-lab\niface lab31 inet static\n        address 192.168.50.1/23\n# lab.sh net 31 end\n")


@pytest.fixture(autouse=True)
def ifaces(tmp_path, monkeypatch):
    p = tmp_path / "interfaces"
    p.write_text(OLD + "\n" + NEW)
    monkeypatch.setattr(config, "IFACES", str(p))
    monkeypatch.setattr(nets, "_registered", {})


def test_old_net_keeps_its_name_and_range():
    assert nets.net_name("30") == "net30" and str(nets.cidr("30")) == "10.10.30.0/24"
    assert str(nets.gateway("30")) == "10.10.30.1"
    assert nets.net_name("dhcp") == "vmbr1(dhcp)" and str(nets.cidr("dhcp")) == "10.10.10.0/24"


def test_named_net_from_its_block():
    assert nets.net_name("31") == "docker-lab" and str(nets.cidr("31")) == "192.168.50.0/23"
    assert nets.resolve("docker-lab") == "31" and nets.resolve("net30") == "30" and nets.resolve("nope") is None
    assert nets.all_nets()["31"] == ("docker-lab", ipaddress.ip_network("192.168.50.0/23"))


def test_vm_addresses_cross_the_third_byte_in_a_23():
    a = list(map(str, nets.vm_addresses("31")))
    assert a[0] == "192.168.50.11" and "192.168.50.255" in a and "192.168.51.0" in a and a[-1] == "192.168.51.254"


def test_registered_net_wins_before_its_block_exists():
    nets.register("32", "web", ipaddress.ip_network("172.20.0.0/27"))
    assert nets.net_name("32") == "web" and len(list(nets.vm_addresses("32"))) == 20


@pytest.mark.parametrize("name,n,why", [("net31", "32", "looks like"), ("40", "32", "start with a letter"),
    ("dhcp", "32", "reserved"), ("docker-lab", "32", "already"), ("Bad", "32", "lowercase"), ("a-", "32", "end"),
    ("net32", "32", None), ("web-2", "32", None)])
def test_name_rules(name, n, why):
    got = nets.name_problem(name, n)
    assert got is None if why is None else why in got


@pytest.mark.parametrize("v,why", [("192.168.50.7/24", "192.168.50.0/24"), ("8.8.8.0/24", "private"), ("127.0.0.0/16", "private"), ("169.254.0.0/16", "private"),
    ("10.0.0.0/8", "/16"), ("10.1.0.0/28", "/27"), ("nonsense", "CIDR")])
def test_bad_cidr_dies_with_the_reason(v, why):
    with pytest.raises(LabError, match=why.replace(".", r"\.")):
        nets.parse_cidr(v)


def test_good_cidr():
    assert str(nets.parse_cidr("192.168.50.0/24")) == "192.168.50.0/24"


def test_overlap_names_the_range():
    others = {"vmbr1": ipaddress.ip_network("10.10.10.0/24"), "route on workshop2": ipaddress.ip_network("10.0.0.0/24")}
    assert "route on workshop2" in nets.overlap(ipaddress.ip_network("10.0.0.0/23"), "32", others)
    assert nets.overlap(ipaddress.ip_network("192.168.60.0/24"), "32", others) is None


OLD_STANZA = 'auto vmbr30\niface vmbr30 inet static\n        address 10.10.30.1/24\n        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n        post-up   sysctl -qw net.ipv4.ip_forward=1\n        post-up   iptables -t nat -C POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE\n        post-up   iptables -t mangle -C FORWARD -i vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || iptables -t mangle -A FORWARD -i vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-up   iptables -t mangle -C FORWARD -o vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || iptables -t mangle -A FORWARD -o vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-up   iptables -C FORWARD -i vmbr30 -o vmbr30 -j ACCEPT 2>/dev/null || iptables -A FORWARD -i vmbr30 -o vmbr30 -j ACCEPT\n        post-up   iptables -C FORWARD -i vmbr30 -o vmbr0 -j ACCEPT 2>/dev/null || iptables -A FORWARD -i vmbr30 -o vmbr0 -j ACCEPT\n        post-up   iptables -C FORWARD -o vmbr30 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -A FORWARD -o vmbr30 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n        post-up   iptables -C FORWARD -i vmbr30 -j DROP 2>/dev/null || iptables -A FORWARD -i vmbr30 -j DROP\n        post-up   iptables -C FORWARD -o vmbr30 -j DROP 2>/dev/null || iptables -A FORWARD -o vmbr30 -j DROP\n        post-down iptables -t nat -D POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE\n        post-down iptables -t mangle -D FORWARD -i vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-down iptables -t mangle -D FORWARD -o vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-down iptables -D FORWARD -i vmbr30 -o vmbr30 -j ACCEPT\n        post-down iptables -D FORWARD -i vmbr30 -o vmbr0 -j ACCEPT\n        post-down iptables -D FORWARD -o vmbr30 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n        post-down iptables -D FORWARD -i vmbr30 -j DROP\n        post-down iptables -D FORWARD -o vmbr30 -j DROP\n'


def test_stanza_of_a_23_net():
    nets.register("31", "docker-lab", ipaddress.ip_network("192.168.50.0/23"))
    s = networks.bridge_stanza("31", "vmbr31", [])
    assert nets.NAME_LINE.format("docker-lab") in s and "address 192.168.50.1/23" in s
    assert "-s 192.168.50.0/23 " in s and "MASQUERADE" in s


def test_old_net_stanza_is_unchanged():
    assert networks.bridge_stanza("30", "vmbr30", networks.net_rules("vmbr30")) == OLD_STANZA
    assert "pvelab name" not in OLD_STANZA


def test_dhcp_stanza_has_no_name_line():
    s = networks.bridge_stanza("dhcp", "vmbr1", [])
    assert "pvelab name" not in s and "address 10.10.10.1/24" in s


def test_next_ip_skips_used_and_goes_past_255():
    rows = {i: {"net": "31", "ip": f"192.168.50.{i}"} for i in range(11, 255)}
    assert networks.next_ip("31", rows) == "192.168.50.255"
    rows[1000] = {"net": "31", "ip": "192.168.50.255"}
    assert networks.next_ip("31", rows) == "192.168.51.0"


def test_next_ip_full(monkeypatch):
    nets.register("32", "tiny", ipaddress.ip_network("172.20.0.0/27"))
    rows = {i: {"net": "32", "ip": str(a)} for i, a in enumerate(nets.vm_addresses("32"))}
    with pytest.raises(LabError, match=r"tiny \(172.20.0.0/27\) is full"):
        networks.next_ip("32", rows)


def test_cloud_init_ip_line():
    assert lifecycle.ip_config("31", "192.168.50.11") == "ip=192.168.50.11/23,gw=192.168.50.1"
    assert lifecycle.ip_config("30", "10.10.30.11") == "ip=10.10.30.11/24,gw=10.10.30.1"
    assert lifecycle.ip_config("dhcp", "") == "ip=dhcp"


def test_sdn_vnet_args(monkeypatch):
    calls = []
    monkeypatch.setattr(sdn, "run", lambda *a, **k: calls.append([str(x) for x in a]) or "")
    monkeypatch.setattr(sdn, "ensure_zone", lambda: None)
    sdn.add_vnet("31")
    flat = [" ".join(c) for c in calls]
    assert any("--subnet 192.168.50.0/23 --gateway 192.168.50.1" in c for c in flat)
    assert any("--alias pvelab docker-lab" in c for c in flat)
    assert sdn.subnet_id("30") == "pvelab-10.10.30.0-24"
    assert sdn.subnet_id("31") == "pvelab-192.168.50.0-23"


def test_isolation_arp_rule_uses_the_gateway():
    assert "arp daddr ip 192.168.50.1 accept" in isolation.nft_table("31", {}, gateway=False)


def test_remove_bridge_deletes_the_custom_subnet_by_its_real_id(monkeypatch):
    calls = []
    monkeypatch.setattr(networks, "bridge_is_ours", lambda n: True)
    monkeypatch.setattr(networks, "is_sdn", lambda n: True)
    monkeypatch.setattr(networks, "SETTERS", {})
    monkeypatch.setattr(ifaces_mod, "IFACES", config.IFACES)
    monkeypatch.setattr(ifaces_mod, "run", lambda *a, **k: "")                  # the .lab-bak copy
    monkeypatch.setattr(networks.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""))   # ifdown
    monkeypatch.setattr(networks, "link_exists", lambda br: False)
    monkeypatch.setattr(sdn, "run", lambda *a, **k: calls.append(" ".join(str(x) for x in a)) or "")
    assert networks.remove_bridge("31", quiet=True) is True
    assert any("subnets/pvelab-192.168.50.0-23" in c for c in calls)
    assert not any("10.10.31" in c for c in calls)
    assert "lab.sh net 31 begin" not in open(config.IFACES).read()


# --- a CIDR must not collide with anything (Task 3)

ROUTES = ("default via 192.0.2.254 dev vmbr0\n10.0.0.0/24 dev vmbr0 proto kernel\n"
          "10.10.40.0/24 dev lab40 proto kernel\n169.254.0.0/16 dev eth9\n10.1.1.1/32 dev tun0\n")


def fake_cluster(monkeypatch, routes, online=("workshop1", "workshop2")):
    from pvelab import nodes
    members = {n: {"ip": "192.0.2.1", "online": True} for n in online}
    members["offline1"] = {"ip": "192.0.2.9", "online": False}
    read = []
    monkeypatch.setattr(nodes, "members", lambda: members)

    def run_on(node, *cmd, **kw):
        read.append(node)
        return routes.get(node, "")
    monkeypatch.setattr(nodes, "run_on", run_on)
    return read


def test_parse_routes():
    assert nets.parse_routes(ROUTES) == [ipaddress.ip_network(x) for x in
                                         ("10.0.0.0/24", "10.10.40.0/24", "169.254.0.0/16")]


def test_route_on_the_other_host_is_refused(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "default via 1.1.1.1 dev vmbr0\n",
                               "workshop2": "10.20.0.0/24 dev vmbr0\n"})
    with pytest.raises(LabError, match=r"route on workshop2.*10\.20\.0\.0/24|10\.20\.0\.0/23 overlaps route on workshop2"):
        nets.cidr_free("33", ipaddress.ip_network("10.20.0.0/23"))


def test_offline_node_is_skipped_not_fatal(monkeypatch):
    read = fake_cluster(monkeypatch, {"workshop1": "10.0.0.0/24 dev vmbr0\n", "workshop2": "10.0.1.0/24 dev vmbr0\n"})
    nets.cidr_free("33", ipaddress.ip_network("10.30.0.0/24"))
    assert "offline1" not in read


def test_node_that_does_not_answer_warns(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "10.0.0.0/24 dev vmbr0\n"})
    warned = []
    monkeypatch.setattr(nets, "warn", warned.append)
    nets.cidr_free("33", ipaddress.ip_network("10.30.0.0/24"))
    assert warned == ["routes on workshop2 not checked: no answer"]


def test_own_bridge_route_does_not_count(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "192.168.50.0/23 dev lab31 proto kernel\n", "workshop2": "x\n"})
    nets.cidr_free("31", nets.cidr("31"))


def test_vmbr1_and_other_nets_are_taken(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "x\n"})
    with pytest.raises(LabError, match=r"overlaps vmbr1"):
        nets.cidr_free("33", ipaddress.ip_network("10.10.10.0/25"))
    with pytest.raises(LabError, match=r"overlaps net30"):
        nets.cidr_free("33", ipaddress.ip_network("10.10.30.0/25"))


def test_sdn_subnet_of_another_vnet_is_taken(monkeypatch, tmp_path):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "x\n"})
    p = tmp_path / "subnets.cfg"
    p.write_text("subnet: office-192.168.51.0-24\n\tvnet office\n\tgateway 192.168.51.1\n\n"
                 "subnet: pvelab-192.168.60.0-24\n\tvnet lab33\n\tgateway 192.168.60.1\n")
    monkeypatch.setattr(config, "SDN_SUBNETS", str(p))
    assert "SDN subnet office-192.168.51.0-24" in nets.taken_ranges("33")
    assert not any("192.168.60" in k for k in nets.taken_ranges("33"))


def test_list_warns_when_someone_overlaps_a_net(monkeypatch, capsys):
    from pvelab.commands import listing
    monkeypatch.setattr(nets, "taken_ranges", lambda n: {"SDN subnet office-192.168.51.0-24":
                                                         ipaddress.ip_network("192.168.51.0/24")} if n == "31" else {})
    monkeypatch.setattr(listing, "is_isolated", lambda n: True)
    monkeypatch.setattr(listing, "host_mem_mb", lambda: (1024, 1024))
    vms = {1: {"name": "a"}}
    listing.summary(vms, {}, {}, (10, 100), {"31"}, set(), True)
    assert ("docker-lab (192.168.50.0/23) overlaps SDN subnet office-192.168.51.0-24 (192.168.51.0/24): "
            "VMs may not reach each other or the internet") in capsys.readouterr().out


def test_targets_by_net_name(monkeypatch):
    from pvelab import inventory
    man = {201: {"name": "docker-lab-student01", "description": "net=31"},
           202: {"name": "net30-student01", "description": "net=30"}}
    monkeypatch.setattr(inventory, "managed", lambda: man)
    monkeypatch.setattr(inventory, "cred_rows", lambda: {})
    assert inventory.targets(["--net", "docker-lab"]) == inventory.targets(["--net", "31"]) == [201]
    assert inventory.targets(["--net", "net30"]) == [202]


def test_unknown_net_name():
    from pvelab import inventory
    with pytest.raises(LabError, match="no net called 'nope'"):
        inventory.targets(["--net", "nope"])


def test_list_summary_line_names_the_range(monkeypatch, capsys):
    from pvelab.commands import listing
    monkeypatch.setattr(listing, "is_isolated", lambda n: True)
    monkeypatch.setattr(listing, "host_mem_mb", lambda: (1024, 1024))
    monkeypatch.setattr(nets, "taken_ranges", lambda n: {})
    listing.summary({1: {"name": "a"}}, {}, {}, (10, 100), {"30", "31"}, set(), True)
    out = capsys.readouterr().out
    assert "docker-lab (192.168.50.0/23): students isolated from each other" in out
    assert "net30 (10.10.30.0/24): students isolated from each other" in out


def test_hint_names_the_net(monkeypatch, capsys):
    from pvelab.commands import listing
    monkeypatch.setattr(listing, "is_isolated", lambda n: False)
    monkeypatch.setattr(listing, "host_mem_mb", lambda: (1024, 1024))
    monkeypatch.setattr(nets, "taken_ranges", lambda n: {})
    listing.summary({1: {"name": "a"}}, {}, {}, (10, 100), {"31"}, set(), True)
    assert "(pvelab isolate --net docker-lab)" in capsys.readouterr().out


def test_lab_data_offers_names(monkeypatch):
    from pvelab import inventory
    man = {201: {"name": "x", "description": "net=31"}, 202: {"name": "y", "description": "net=30"}}
    monkeypatch.setattr(inventory, "managed", lambda: man)
    monkeypatch.setattr(inventory, "cred_rows", lambda: {})
    monkeypatch.setattr(inventory, "node_names", lambda: [])
    d = inventory.lab_data()
    assert d["nets"] == ["net30", "docker-lab"] and d["count"] == {"net30": 1, "docker-lab": 1}


def test_net_label_is_the_name():
    assert networks.net_label("31") == "docker-lab" and networks.net_label("30") == "net30"
    assert networks.net_label("dhcp") == "vmbr1(dhcp)"


def test_wizard_list_accepts_number_netn_and_name():
    from pvelab import wizard
    w = wizard.Wizard({"nets": ["net30", "docker-lab"], "count": {}})
    assert all(w.has_net(a) for a in ("30", "net30", "31", "net31", "docker-lab"))
    assert not w.has_net("32") and not w.has_net("nope")
