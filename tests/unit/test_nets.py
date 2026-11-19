import ipaddress

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


def test_all_nets_reads_the_interfaces_file_once(monkeypatch):
    read, real = [], config.read_text
    monkeypatch.setattr(config, "read_text", lambda path: read.append(path) or real(path))
    assert nets.all_nets() == {"30": ("net30", ipaddress.ip_network("10.10.30.0/24")),
                               "31": ("docker-lab", ipaddress.ip_network("192.168.50.0/23"))}
    assert read.count(config.IFACES) == 1


def test_vm_addresses_cross_the_third_byte_in_a_23():
    a = list(map(str, nets.vm_addresses("31")))
    assert a[0] == "192.168.50.11" and "192.168.50.255" in a and "192.168.51.0" in a and a[-1] == "192.168.51.254"


def test_registered_net_wins_before_its_block_exists():
    nets.register("32", "web", ipaddress.ip_network("172.20.0.0/27"))
    assert nets.net_name("32") == "web" and len(list(nets.vm_addresses("32"))) == 20


@pytest.mark.parametrize("name,n,why", [("net31", "32", "looks like"), ("40", "32", "start with a letter"),
    ("dhcp", "32", "reserved"), ("docker-lab", "32", "already"), ("Bad", "32", "lowercase"), ("a-", "32", "end"),
    ("net32", "32", None), ("web-2", "32", None), ("a" * 31, "32", "30 characters"), ("a" * 30, "32", None),
    ("auto", "32", "reserved"), ("all", "32", "reserved"), ("vmbr1", "32", "bridge"), ("lab5", "32", "bridge"),
    ("vmbr", "32", None), ("labx", "32", None)])
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
    assert "route on workshop2" in nets.overlap(ipaddress.ip_network("10.0.0.0/23"), others)
    assert nets.overlap(ipaddress.ip_network("192.168.60.0/24"), others) is None


OLD_STANZA = 'auto vmbr30\niface vmbr30 inet static\n        address 10.10.30.1/24\n        bridge-ports none\n        bridge-stp off\n        bridge-fd 0\n        post-up   sysctl -qw net.ipv4.ip_forward=1\n        post-up   iptables -t nat -C POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE\n        post-up   iptables -t mangle -C FORWARD -i vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || iptables -t mangle -A FORWARD -i vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-up   iptables -t mangle -C FORWARD -o vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || iptables -t mangle -A FORWARD -o vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-up   iptables -C FORWARD -i vmbr30 -o vmbr30 -j ACCEPT 2>/dev/null || iptables -A FORWARD -i vmbr30 -o vmbr30 -j ACCEPT\n        post-up   iptables -C FORWARD -i vmbr30 -o vmbr0 -j ACCEPT 2>/dev/null || iptables -A FORWARD -i vmbr30 -o vmbr0 -j ACCEPT\n        post-up   iptables -C FORWARD -o vmbr30 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -A FORWARD -o vmbr30 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n        post-up   iptables -C FORWARD -i vmbr30 -j DROP 2>/dev/null || iptables -A FORWARD -i vmbr30 -j DROP\n        post-up   iptables -C FORWARD -o vmbr30 -j DROP 2>/dev/null || iptables -A FORWARD -o vmbr30 -j DROP\n        post-down iptables -t nat -D POSTROUTING -s 10.10.30.0/24 -o vmbr0 -j MASQUERADE\n        post-down iptables -t mangle -D FORWARD -i vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-down iptables -t mangle -D FORWARD -o vmbr30 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu\n        post-down iptables -D FORWARD -i vmbr30 -o vmbr30 -j ACCEPT\n        post-down iptables -D FORWARD -i vmbr30 -o vmbr0 -j ACCEPT\n        post-down iptables -D FORWARD -o vmbr30 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n        post-down iptables -D FORWARD -i vmbr30 -j DROP\n        post-down iptables -D FORWARD -o vmbr30 -j DROP\n'


def test_stanza_of_a_23_net():
    nets.register("31", "docker-lab", ipaddress.ip_network("192.168.50.0/23"))
    s = networks.bridge_stanza("31", "vmbr31", [])
    assert nets.NAME_LINE.format("docker-lab") in s and "address 192.168.50.1/23" in s
    assert "-s 192.168.50.0/23 " in s and "MASQUERADE" in s


def test_old_net_stanza_is_unchanged():
    assert networks.bridge_stanza("30", "vmbr30", networks.net_rules("vmbr30")) == OLD_STANZA.replace("iptables ", "iptables -w 10 ")
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
    assert any("--alias docker-lab" in c for c in flat)


def test_isolation_arp_rule_uses_the_gateway():
    assert "arp daddr ip 192.168.50.1 accept" in isolation.nft_table("31", {}, gateway=False)


def test_remove_bridge_deletes_the_custom_subnet_by_its_real_id(monkeypatch):
    calls = []
    monkeypatch.setattr(networks, "bridge_is_ours", lambda n: True)
    monkeypatch.setattr(networks, "is_sdn", lambda n: True)
    monkeypatch.setattr(networks, "SETTERS", {})
    monkeypatch.setattr(ifaces_mod, "run", lambda *a, **k: "")                  # the .lab-bak copy
    monkeypatch.setattr(networks, "run", lambda *a, **k: "")                    # ifdown
    monkeypatch.setattr(networks, "link_exists", lambda br: False)
    monkeypatch.setattr(sdn, "run", lambda *a, **k: calls.append(" ".join(str(x) for x in a)) or "")
    with open(config.SDN_SUBNETS, "w") as f:      # read from subnets.cfg: the net block is gone first
        f.write("subnet: pvelab-192.168.50.0-23\n\tvnet lab31\n")
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


def test_route_lines():
    assert [net for net, _, _ in nets._route_lines(ROUTES)] == [ipaddress.ip_network(x) for x in
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


def test_node_without_an_address_warns_not_dies(monkeypatch):
    from pvelab import nodes
    monkeypatch.setattr(nodes, "members", lambda: {"workshop1": {"ip": "192.0.2.1", "online": True},
                                                   "workshop2": {"ip": None, "online": True}})
    monkeypatch.setattr(nodes, "is_local", lambda n: n == "workshop1")
    monkeypatch.setattr(nodes, "run", lambda *c, **k: "10.0.0.0/24 dev vmbr0\n")
    warned = []
    monkeypatch.setattr(nets, "warn", warned.append)
    nets.cidr_free("33", ipaddress.ip_network("10.30.0.0/24"))
    assert warned == ["routes on workshop2 not checked: no answer"]


def test_routes_are_read_with_a_timeout_on_a_connection_of_their_own(monkeypatch):
    kws = []
    from pvelab import nodes
    fake_cluster(monkeypatch, {})
    monkeypatch.setattr(nodes, "run_on", lambda node, *c, **k: kws.append(k) or "x\n")
    nets.taken_ranges("33")
    assert kws and all(k.get("shared") is False and k.get("timeout") for k in kws)


def test_own_bridge_route_does_not_count(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "192.168.50.0/23 dev lab31 proto kernel\n", "workshop2": "x\n"})
    nets.cidr_free("31", nets.cidr("31"))


def test_vmbr1_and_other_nets_are_taken(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "x\n"})
    with pytest.raises(LabError, match=r"overlaps vmbr1"):
        nets.cidr_free("33", ipaddress.ip_network("10.10.10.0/25"))
    with pytest.raises(LabError, match=r"overlaps net30"):
        nets.cidr_free("33", ipaddress.ip_network("10.10.30.0/25"))


def test_a_net_called_like_vmbr1_cannot_hide_it(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "x\n"})
    nets.register("34", "vmbr1", ipaddress.ip_network("172.20.0.0/24"))
    monkeypatch.setattr(nets, "all_nets", lambda: {"34": ("vmbr1", ipaddress.ip_network("172.20.0.0/24"))})
    t = nets.taken_ranges("33")
    assert ipaddress.ip_network("10.10.10.0/24") in t.values() and ipaddress.ip_network("172.20.0.0/24") in t.values()


def test_free_net_skips_a_number_whose_range_a_custom_net_covers(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "x\n"})
    monkeypatch.setattr(networks, "link_exists", lambda dev: False)
    monkeypatch.setattr(networks.sdn, "conflict", lambda n, subnet=True: None)
    monkeypatch.setattr(networks, "guest_conf_files", lambda: [])
    monkeypatch.setattr(networks, "cred_rows", lambda: {})
    monkeypatch.setattr(nets, "all_nets", lambda: {"31": ("docker-lab", ipaddress.ip_network("10.10.0.0/22"))})
    assert networks.free_net() == "4"
    assert networks.free_net(True) == "2"


def test_free_net_skips_a_routed_default_range_unless_custom(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "10.10.2.0/24 dev eth9 proto kernel\n"})
    monkeypatch.setattr(networks, "link_exists", lambda dev: False)
    monkeypatch.setattr(networks.sdn, "conflict", lambda n, subnet=True: None)
    monkeypatch.setattr(networks, "guest_conf_files", lambda: [])
    monkeypatch.setattr(networks, "cred_rows", lambda: {})
    assert networks.free_net(True) == "2"
    assert networks.free_net() == "3"


def test_foreign_use_names_the_host_that_routes_the_range(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "x\n", "workshop2": "10.10.2.0/24 dev eth9 proto kernel\n"})
    monkeypatch.setattr(networks, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(networks, "link_exists", lambda dev: False)
    monkeypatch.setattr(networks.sdn, "conflict", lambda n, subnet=True: None)
    monkeypatch.setattr(networks, "guest_conf_files", lambda: [])
    assert networks.foreign_use("2") == "10.10.2.0/24 overlaps route on workshop2 (10.10.2.0/24)"


def test_a_net_without_a_number_owns_no_bridge(monkeypatch, tmp_path):
    """The wizard checks a new net's range before it has a number: vmbr0's LAN is not 'its own bridge'."""
    fake_cluster(monkeypatch, {"workshop1": "10.0.0.0/24 dev vmbr0 proto kernel\n10.40.0.0/24 dev lab0\n",
                               "workshop2": "x\n"})
    p = tmp_path / "subnets.cfg"
    p.write_text("subnet: pvelab-10.50.0.0-24\n\tvnet lab0\n")
    monkeypatch.setattr(config, "SDN_SUBNETS", str(p))
    for taken in ("10.0.0.0/24", "10.40.0.0/24", "10.50.0.0/24"):
        with pytest.raises(LabError, match="overlaps"):
            nets.cidr_free(None, ipaddress.ip_network(taken))
    nets.cidr_free(None, ipaddress.ip_network("10.60.0.0/24"))


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
    monkeypatch.setattr(nets, "taken_ranges", lambda n, net=None: {"SDN subnet office-192.168.51.0-24":
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
    monkeypatch.setattr(nets, "taken_ranges", lambda n, net=None: {})
    listing.summary({1: {"name": "a"}}, {}, {}, (10, 100), {"30", "31"}, set(), True)
    out = capsys.readouterr().out
    assert "docker-lab (192.168.50.0/23): owners isolated from each other" in out
    assert "net30 (10.10.30.0/24): owners isolated from each other" in out


def test_hint_names_the_net(monkeypatch, capsys):
    from pvelab.commands import listing
    monkeypatch.setattr(listing, "is_isolated", lambda n: False)
    monkeypatch.setattr(listing, "host_mem_mb", lambda: (1024, 1024))
    monkeypatch.setattr(nets, "taken_ranges", lambda n, net=None: {})
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


def test_net_name_is_the_label():
    assert nets.net_name("31") == "docker-lab" and nets.net_name("30") == "net30"
    assert nets.net_name("dhcp") == "vmbr1(dhcp)"


def test_wizard_list_picks_a_net_from_a_list(monkeypatch):
    from pvelab import wizard
    data = {"vms": ["a", "b", "c"], "nets": ["net30", "vmbr1(dhcp)"], "count": {"net30": 2, "vmbr1(dhcp)": 1}}
    labels = []
    def pick(*starts):
        it = iter(starts)
        def select(p, l, d=0, esc="back"):
            labels.append(l)
            start = next(it)
            return next(i for i, x in enumerate(l) if x.startswith(start))
        monkeypatch.setattr(wizard, "select", select)
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    for net, want in (("all", ["list"]), ("net30", ["list", "--net", "net30"]),
                      ("vmbr1", ["list", "--net", "dhcp"])):
        pick(net, "the VMs")
        assert wizard.Wizard(data).list() == want
    assert labels[0] == ["all networks (3 VMs)", "net30 (2 VMs)", "vmbr1(dhcp) (1 VM)"]
    pick("net30", "each VM's ssh")                         # one line per VM to hand out: no passwords question
    assert wizard.Wizard(data).list() == ["list", "--net", "net30", "--ssh"]
    monkeypatch.setattr("builtins.input", lambda p="": "y")
    pick("the VMs")                                        # one net: not asked which
    one = {"vms": ["a"], "nets": ["net2"], "count": {"net2": 1}}
    assert wizard.Wizard(one).list() == ["list", "--passwords"]


def test_a_registered_net_without_its_block_is_listed_and_resolved():
    nets.register("33", "web", ipaddress.ip_network("172.20.0.0/27"))
    assert nets.all_nets()["33"] == ("web", ipaddress.ip_network("172.20.0.0/27"))
    assert nets.resolve("web") == "33" and "already called web" in nets.name_problem("web", "34")


def test_a_net_of_another_node_comes_from_its_sdn_vnet(monkeypatch, tmp_path):
    v, s = tmp_path / "vnets.cfg", tmp_path / "subnets.cfg"
    v.write_text("vnet: lab35\n\tzone pvelab\n\talias pvelab web-lab\n\ttag 4200035\n\n"
                 "vnet: lab36\n\tzone office\n\talias pvelab other\n\n"
                 "vnet: lab37\n\tzone pvelab\n\talias pvelab net37\n")
    s.write_text("subnet: pvelab-172.20.0.0-23\n\tvnet lab35\n\tgateway 172.20.0.1\n\n"
                 "subnet: pvelab-10.10.37.0-24\n\tvnet lab37\n\tgateway 10.10.37.1\n")
    monkeypatch.setattr(config, "SDN_VNETS", str(v))
    monkeypatch.setattr(config, "SDN_SUBNETS", str(s))
    assert nets.net_name("35") == "web-lab" and str(nets.cidr("35")) == "172.20.0.0/23"
    assert nets.resolve("web-lab") == "35" and nets.resolve("other") is None
    assert nets.all_nets()["37"] == ("net37", ipaddress.ip_network("10.10.37.0/24"))
    assert "36" not in nets.all_nets()                   # another zone's vnet is not pvelab's
    assert nets.net_name("31") == "docker-lab"           # the block wins where there is one


def test_a_broad_route_through_a_gateway_does_not_block_a_net(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "10.0.0.0/8 via 192.0.2.254 dev wg0 proto static\n"
                                            "172.16.0.0/12 dev wg1 scope link\n", "workshop2": "x\n"})
    nets.cidr_free("33", ipaddress.ip_network("10.10.33.0/24"))
    nets.cidr_free("33", ipaddress.ip_network("172.20.0.0/24"))
    assert not nets.overlaps("30")
    with pytest.raises(LabError, match="route on workshop1"):    # the same range, or a wider one, still does
        nets.cidr_free("33", ipaddress.ip_network("172.16.0.0/12"))


def test_a_host_network_that_covers_the_range_still_blocks_it(monkeypatch):
    fake_cluster(monkeypatch, {"workshop1": "10.0.0.0/24 dev vmbr0 proto kernel scope link src 10.0.0.123\n",
                               "workshop2": "x\n"})
    with pytest.raises(LabError, match="route on workshop1"):
        nets.cidr_free("33", ipaddress.ip_network("10.0.0.0/27"))


def test_dhcp_range_comes_from_dhcp_net(monkeypatch):
    monkeypatch.setattr(config, "DHCP_NET", "10.99.1")
    assert str(nets.default_cidr("dhcp")) == "10.99.1.0/24"


# --- a chosen gateway (.254) and the vnet alias

def test_parse_gateway_forms_and_limits():
    net = ipaddress.ip_network("10.5.0.0/24")
    assert nets.parse_gateway(".254", net) == ipaddress.ip_address("10.5.0.254")
    assert nets.parse_gateway("10.5.0.1", net) == ipaddress.ip_address("10.5.0.1")
    for bad in ("10.5.1.254", ".255", ".0", ".300", "x"):
        with pytest.raises(LabError):
            nets.parse_gateway(bad, net)


def test_registered_gateway_is_skipped_by_vm_addresses():
    nets.register("77", "prod", ipaddress.ip_network("10.5.0.0/27"), ipaddress.ip_address("10.5.0.30"))
    try:
        assert nets.gateway("77") == ipaddress.ip_address("10.5.0.30")
        got = [str(a) for a in nets.vm_addresses("77")]
        assert got[0] == "10.5.0.11" and got[-1] == "10.5.0.29" and "10.5.0.30" not in got
    finally:
        nets._registered.pop("77", None)


def test_gateway_from_the_block_and_from_the_sdn_subnet(monkeypatch, tmp_path):
    monkeypatch.setattr(nets, "_block", lambda n: "iface lab5 inet static\n        address 10.5.1.254/24\n"
                        if n == "5" else None)
    assert nets.gateway("5") == ipaddress.ip_address("10.5.1.254")
    v, sub = tmp_path / "vnets.cfg", tmp_path / "subnets.cfg"
    v.write_text("vnet: lab6\n\tzone pvelab\n\talias prod\n\n")
    sub.write_text("subnet: pvelab-10.5.0.0-24\n\tvnet lab6\n\tgateway 10.5.0.254\n\n")
    monkeypatch.setattr(config, "SDN_VNETS", str(v))
    monkeypatch.setattr(config, "SDN_SUBNETS", str(sub))
    assert nets.net_name("6") == "prod"              # the alias alone is the name (older: "pvelab NAME")
    assert nets.gateway("6") == ipaddress.ip_address("10.5.0.254")
    assert nets.gateway("7") == ipaddress.ip_address("10.10.7.1")     # nothing chosen: .1


# --- net set: a network's range or gateway changed with its VMs

def test_new_ips_keep_their_place_when_it_fits():
    from pvelab.readdress import new_ips
    old, new = ipaddress.ip_network("10.10.2.0/24"), ipaddress.ip_network("10.5.0.0/24")
    got = new_ips({201: "10.10.2.11", 202: "10.10.2.12", 203: "10.10.2.254"}, old, new,
                  ipaddress.ip_address("10.5.0.254"))
    assert got == {201: "10.5.0.11", 202: "10.5.0.12", 203: "10.5.0.13"}       # .254 is the gateway now


def test_new_ips_into_a_smaller_range_and_no_room():
    from pvelab.readdress import new_ips
    old, small = ipaddress.ip_network("10.10.2.0/24"), ipaddress.ip_network("10.5.0.0/27")
    got = new_ips({1: "10.10.2.11", 2: "10.10.2.200"}, old, small, ipaddress.ip_address("10.5.0.1"))
    assert got == {1: "10.5.0.11", 2: "10.5.0.12"}
    many = {i: f"10.10.2.{10 + i}" for i in range(1, 25)}        # 24 VMs, room for 19 (.11-.30 without gw .30)
    assert new_ips(many, old, small, ipaddress.ip_address("10.5.0.30")) is None


def test_snapshot_sections_get_the_new_ipconfig_but_not_pve_special_ones():
    from pvelab.lifecycle import with_snapshots
    text = ("ipconfig0: ip=10.10.2.11/24,gw=10.10.2.1\nname: a\n\n[clean]\nipconfig0: ip=10.10.2.11/24,gw=10.10.2.1\n"
            "\n[special:cloudinit]\nipconfig0: ip=10.10.2.11/24,gw=10.10.2.1\n")
    out = with_snapshots(text, "ipconfig0", "ip=10.5.0.11/24,gw=10.5.0.254")
    assert out.splitlines()[0] == "ipconfig0: ip=10.10.2.11/24,gw=10.10.2.1"     # qm set changes this one
    assert "[clean]\nipconfig0: ip=10.5.0.11/24,gw=10.5.0.254" in out
    assert "[special:cloudinit]\nipconfig0: ip=10.10.2.11/24,gw=10.10.2.1" in out


def test_rename_helpers():
    from pvelab.readdress import renamed, with_name_line
    assert renamed("net2-student01-2", "net2", "prod") == "prod-student01-2"
    assert renamed("net2-shared", "net2", "prod") == "prod-shared"
    assert renamed("net20-student01", "net2", "prod") == "net20-student01"       # another net's
    old = "# begin\nauto vmbr2\niface vmbr2 inet static\n# end"
    assert with_name_line(old, "prod") == "# begin\n        # pvelab name prod\nauto vmbr2\niface vmbr2 inet static\n# end"
    named = "# begin\n        # pvelab name net2x\niface lab2 inet static\n# end"
    assert with_name_line(named, "prod") == "# begin\n        # pvelab name prod\niface lab2 inet static\n# end"


def test_local_net_refuses_a_gateway_on_another_host(monkeypatch):
    from pvelab import readdress
    monkeypatch.setattr(readdress, "net_arg", lambda v: "5")
    monkeypatch.setattr(readdress, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(readdress.nets, "net_name", lambda n: "prod")
    monkeypatch.setattr(readdress, "bridge", lambda n: "lab5")
    with pytest.raises(LabError, match="run pvelab net rename on the host that has lab5"):
        readdress.local_net("prod", "rename")


def test_vendor_data_keeps_ssh_host_keys():
    from pvelab.templates import vendor_data
    assert "ssh_deletekeys: false" in vendor_data()


def test_gateway_dot_n_past_the_range_says_so():
    net = ipaddress.ip_network("10.5.0.0/24")
    with pytest.raises(LabError, match="not a usable address of 10.5.0.0/24"):
        nets.parse_gateway(".99999999999", net)
    with pytest.raises(LabError, match="not an IPv4 address"):
        nets.parse_gateway("10.5.0", net)


# --- PVE's SDN as the source: vnets made in the web UI

PVE_ZONES = "vxlan: pvelab\n\tpeers 10.0.0.123\n\nsimple: SDC\n\tipam pve\n\n"
PVE_VNETS = ("vnet: lab2\n\tzone pvelab\n\talias pvelab net2\n\n"
             "vnet: vnet1\n\tzone SDC\n\talias prod\n\nvnet: vnet3\n\tzone SDC\n\n")
PVE_SUBNETS = ("subnet: pvelab-10.10.2.0-24\n\tvnet lab2\n\tgateway 10.10.2.1\n\n"
               "subnet: SDC-10.5.0.0-24\n\tvnet vnet1\n\tgateway 10.5.0.254\n\tsnat 1\n\n"
               "subnet: SDC-10.5.2.0-24\n\tvnet vnet3\n\tgateway 10.5.2.254\n\n")


def pve_sdn(monkeypatch, tmp_path):
    for name, text, attr in (("zones.cfg", PVE_ZONES, "SDN_ZONES"), ("vnets.cfg", PVE_VNETS, "SDN_VNETS"),
                             ("subnets.cfg", PVE_SUBNETS, "SDN_SUBNETS")):
        (tmp_path / name).write_text(text)
        monkeypatch.setattr(config, attr, str(tmp_path / name))


def test_pve_vnets_lists_every_vnet_with_its_zone_type_subnet_and_snat(monkeypatch, tmp_path):
    pve_sdn(monkeypatch, tmp_path)
    got = {v["vnet"]: v for v in nets.pve_vnets()}
    assert set(got) == {"lab2", "vnet1", "vnet3"}
    assert got["vnet1"]["type"] == "simple" and got["vnet1"]["alias"] == "prod" and got["vnet1"]["snat"]
    assert str(got["vnet1"]["cidr"]) == "10.5.0.0/24" and str(got["vnet1"]["gateway"]) == "10.5.0.254"
    assert not got["vnet3"]["snat"] and got["vnet3"]["alias"] is None


def test_free_vnets_leave_out_pvelabs_own_and_the_taken_over(monkeypatch, tmp_path):
    pve_sdn(monkeypatch, tmp_path)
    assert [v["vnet"] for v in nets.free_vnets()] == ["vnet1", "vnet3"]
    assert nets.free_vnet("prod")["vnet"] == "vnet1" and nets.free_vnet("vnet3")["vnet"] == "vnet3"
    with open(config.IFACES, "a") as f:
        f.write("\n# lab.sh net 5 begin (managed by lab)\n        # pvelab name prod\niface vnet1 inet static\n"
                "# lab.sh net 5 end\n")
    assert [v["vnet"] for v in nets.free_vnets()] == ["vnet3"]
    assert config.bridge("5") == "vnet1" and config.taken_over("5") and not config.is_sdn("5")
    assert nets.net_name("5") == "prod" and str(nets.cidr("5")) == "10.5.0.0/24"     # from PVE's subnet
    assert str(nets.gateway("5")) == "10.5.0.254" and nets.pve_gateway("5")


def test_a_taken_over_vnet_with_pve_gateway_gets_no_address_and_no_nat_of_pvelab(monkeypatch, tmp_path):
    from pvelab import networks
    pve_sdn(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "TAKING", {"5": "vnet1"})
    nets.register("5", "prod", ipaddress.ip_network("10.5.0.0/24"), ipaddress.ip_address("10.5.0.254"))
    stanza = networks.bridge_stanza("5", "vnet1", networks.net_rules("vnet1"), sdn_made=True)
    assert "iface vnet1 inet static" in stanza and "address" not in stanza and "MASQUERADE" not in stanza
    assert "FORWARD -i vnet1 -j DROP" in stanza


def test_tidy_snat_keeps_one_of_each_and_drops_the_gone(monkeypatch, tmp_path):
    from pvelab import sdn
    pve_sdn(monkeypatch, tmp_path)
    rule = "POSTROUTING -s 10.5.0.0/24 -o vmbr0 -j SNAT --to-source 10.0.0.123"
    old = "POSTROUTING -s 10.5.9.0/24 -o vmbr0 -j SNAT --to-source 10.0.0.123"
    other = "POSTROUTING -s 192.168.9.0/24 -o vmbr0 -j SNAT --to-source 10.0.0.123"     # not PVE's: kept
    listing = "\n".join(["-P POSTROUTING ACCEPT", f"-A {rule}", f"-A {rule}", f"-A {old}", f"-A {other}"])
    deleted = []

    def run_on(node, *cmd, **k):
        if "-S" in cmd:
            return listing
        deleted.append(" ".join(cmd[cmd.index("-D") + 1:]))
        return ""
    monkeypatch.setattr(sdn, "run_on", run_on)
    sdn.tidy_snat(gone=[ipaddress.ip_network("10.5.9.0/24")], node="workshop1")
    assert deleted == [rule, old]


def test_a_vnet_another_host_took_over_is_known_here(monkeypatch, tmp_path):
    pve_sdn(monkeypatch, tmp_path)
    config.save_taken({"vnet1": {"net": "7", "host": "workshop2"}})
    assert config.bridge("7") == "vnet1" and config.taken_over("7") and config.in_sdn("7")
    assert [v["vnet"] for v in nets.free_vnets()] == ["vnet3"]          # vnet1 is pvelab's now
    assert nets.net_name("7") == "prod" and str(nets.cidr("7")) == "10.5.0.0/24"
    assert nets.resolve("prod") == "7" and nets.free_vnet("prod") is None
    from pvelab import networks
    assert networks.net_in_use("7") == "its number is held by PVE vnet vnet1, which pvelab took over"


def test_subnet_problem_names_what_pvelab_would_lose(monkeypatch, tmp_path):
    pve_sdn(monkeypatch, tmp_path)
    assert nets.subnet_problem("vnet1") is None
    (tmp_path / "subnets.cfg").write_text(PVE_SUBNETS.replace("\tsnat 1\n", "\tsnat 1\n\tdhcp-range start-address=10.5.0.100,end-address=10.5.0.200\n"))
    assert "dhcp-range" in nets.subnet_problem("vnet1")
    (tmp_path / "subnets.cfg").write_text(PVE_SUBNETS + "subnet: SDC-10.6.0.0-24\n\tvnet vnet1\n\n")
    assert nets.subnet_problem("vnet1").startswith("vnet1 has 2 subnets: change it in PVE")


def test_next_range_keeps_the_gateways_place_or_none():
    old, gw = ipaddress.ip_network("10.5.0.0/24"), ipaddress.ip_address("10.5.0.254")
    assert nets.next_range(old, gw, "10.6.0.0/24", None) == (ipaddress.ip_network("10.6.0.0/24"),
                                                            ipaddress.ip_address("10.6.0.254"))
    assert nets.next_range(old, gw, None, ".1")[1] == ipaddress.ip_address("10.5.0.1")
    assert nets.next_range(old, gw, "10.6.0.0/25", None)[1] == ipaddress.ip_address("10.6.0.1")   # .254 not in a /25
    assert nets.next_range(old, None, "10.6.0.0/24", None)[1] is None       # no gateway stays none


def test_a_taken_over_vnet_whose_subnet_was_deleted_in_pve_says_so(monkeypatch, tmp_path):
    pve_sdn(monkeypatch, tmp_path)
    with open(config.IFACES, "a") as f:
        f.write("\n# lab.sh net 5 begin (managed by lab)\n        # pvelab name prod\niface vnet1 inet static\n"
                "# lab.sh net 5 end\n")
    (tmp_path / "subnets.cfg").write_text("")
    with pytest.raises(LabError, match="has no subnet any more"):
        nets.cidr("5")


def test_pending_changes_of_a_taken_over_vnet_are_pvelabs(monkeypatch):
    from pvelab import sdn
    open(config.SDN_VNETS, "a").write("vnet: vnet1\n\tzone SDC\n\n")
    config.save_taken({"vnet1": {"net": "7", "host": "workshop1"}})
    listings = {"vnets": [{"vnet": "vnet1", "zone": "SDC", "state": "changed"},
                          {"vnet": "vnet3", "zone": "SDC", "state": "new"}]}
    assert sdn.pending(listings) == ["vnet vnet3 (new)"]


def test_a_record_of_a_vnet_deleted_in_pve_is_no_net(monkeypatch, tmp_path):
    pve_sdn(monkeypatch, tmp_path)
    open(config.SDN_VNETS, "a").write("vnet: vnet1\n\tzone SDC\n\n")
    config.save_taken({"vnet1": {"net": "7", "host": "workshop1"}, "gone": {"net": "8", "host": "workshop1"}})
    assert set(config.taken_vnets()) == {"vnet1"}
    assert nets.resolve("gone") is None and config.bridge("8") != "gone"
    monkeypatch.setattr(config, "SDN_RUNNING", str(tmp_path / "running"))
    (tmp_path / "running").write_text('{"vnets": {"ids": {"gone": {"zone": "SDC"}}}}')
    assert set(config.taken_vnets()) == {"vnet1"}              # deleted, not applied yet: no net now ...
    assert config.held_numbers() == {"7": "vnet1", "8": "gone"}   # ... but its number is not free (rollback)
    config.record_taken("vnet3", "9", "workshop1")             # ... but a save keeps it: it may be rolled back
    assert "gone" in open(config.TAKEN_FILE).read()
    (tmp_path / "running").write_text('{"vnets": {"ids": {}}}')     # applied: gone for good
    config.record_taken("vnet3", "9", "workshop1")
    assert "gone" not in open(config.TAKEN_FILE).read()
