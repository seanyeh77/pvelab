"""Isolation on every host of an SDN net: the other hosts' tables, pushing them, and the gateway's fixed MAC."""
import pytest

from pvelab import ifaces, isolation, networks, sdn

from test_nodes import B, cluster

VMS = {201: "net95-student01", 202: "net95-student02"}
GW_RULES = ['oifname "vxlan_lab95" ether daddr 02:00:0a:0a:5f:01 accept',
            'oifname "vxlan_lab95" arp operation request arp daddr ip 10.10.95.1 accept',
            'iifname "vxlan_lab95" ether saddr 02:00:0a:0a:5f:01 accept']


@pytest.fixture
def lab95(monkeypatch):
    monkeypatch.setattr(isolation, "bridge", lambda n: f"lab{n}")


def test_other_hosts_table_lets_vms_reach_only_the_gateway(lab95):
    other, here = isolation.nft_table("95", VMS, gateway=False), isolation.nft_table("95", VMS)
    assert all(r in other for r in GW_RULES) and not any(r in here for r in GW_RULES)
    assert other.index(GW_RULES[-1]) < other.index("        drop\n")       # before the drop
    assert here.replace("".join(f"        {r}\n" for r in GW_RULES), "") == here
    assert other.replace("".join(f"        {r}\n" for r in GW_RULES), "") == here   # nothing else differs


def test_gw_mac():
    assert isolation.gw_mac("95") == "02:00:0a:0a:5f:01" and isolation.gw_mac(2) == "02:00:0a:0a:02:01"


@pytest.fixture
def isolate_here(monkeypatch, tmp_path, lab95):
    """set_isolation's local part, recorded; returns the run_on calls."""
    calls = []
    monkeypatch.setattr(isolation, "NFT_DIR", str(tmp_path / "nft"))
    monkeypatch.setattr(isolation, "cred_rows", lambda: {v: {"name": n, "net": "95"} for v, n in VMS.items()})
    monkeypatch.setattr(isolation, "write_section", lambda *a: calls.append(("section",) + a[:2]))
    monkeypatch.setattr(isolation, "run_on", lambda node, *cmd, **kw: calls.append((node, cmd, kw.get("data"))))
    return calls


@pytest.mark.parametrize("two_hosts, sdn_net, pushed", [(True, True, True), (True, False, False),
                                                        (False, True, False)])
def test_set_isolation_pushes_to_other_hosts(empty_pve, monkeypatch, isolate_here, two_hosts, sdn_net, pushed):
    if two_hosts:
        cluster(empty_pve)
    monkeypatch.setattr(isolation, "is_sdn", lambda n: sdn_net)
    isolation.set_isolation("95", True)
    remote = [c for c in isolate_here if c[0] != "section"]
    assert bool(remote) == pushed
    if pushed:
        node, cmd, data = remote[0]
        assert node == B and len(remote) == 1 and "/etc/pvelab/nft/net95.nft" in cmd[-1] and "nft -f" in cmd[-1]
        assert all(r in data for r in GW_RULES)
    isolate_here.clear()
    isolation.set_isolation("95", False)
    remote = [c for c in isolate_here if c[0] != "section"]
    assert bool(remote) == pushed
    if pushed:
        assert "rm -f /etc/pvelab/nft/net95.nft" in remote[0][1][-1] and "delete table bridge pvelab_net95" in remote[0][1][-1]


def test_set_isolation_skips_offline_hosts(empty_pve, monkeypatch, isolate_here):
    cluster(empty_pve, online_b=False)
    monkeypatch.setattr(isolation, "is_sdn", lambda n: True)
    isolation.set_isolation("95", True)
    assert all(c[0] == "section" for c in isolate_here)


def test_isolation_off_warns_about_an_offline_host(empty_pve, monkeypatch, isolate_here, capsys):
    cluster(empty_pve, online_b=False)
    monkeypatch.setattr(isolation, "is_sdn", lambda n: True)
    isolation.set_isolation("95", False)
    err = capsys.readouterr().err
    assert all(c[0] == "section" for c in isolate_here)
    assert f"host {B} is offline" in err and "rm -f /etc/pvelab/nft/net95.nft" in err


def test_nft_unit_loads_the_tables_at_boot(monkeypatch):
    seen = []
    monkeypatch.setattr(isolation, "run_on", lambda node, *cmd, **kw: seen.append((node, cmd, kw["data"])))
    isolation.ensure_nft_unit("b")
    node, cmd, unit = seen[0]
    assert node == "b" and "systemctl enable pvelab-nft.service" in cmd[-1]
    assert "Type=oneshot" in unit and "After=network.target" in unit and "Before=pve-guests.service" in unit and '"$$f"' in unit
    assert "/etc/pvelab/nft/*.nft" in unit


IFACES_TEXT = """auto lo
iface lo inet loopback

# lab.sh net 95 begin (managed by lab - removed automatically with its last VM)
iface lab95 inet static
        address 10.10.95.1/24
        post-up   sysctl -qw net.ipv4.ip_forward=1
# lab.sh net 95 end
"""


def test_ensure_gw_mac_inserts_once(monkeypatch, tmp_path):
    f = tmp_path / "interfaces"
    f.write_text(IFACES_TEXT)
    ran = []
    for mod in (networks, ifaces):
        monkeypatch.setattr(mod, "IFACES", str(f))
    monkeypatch.setattr(ifaces, "run", lambda *cmd, **kw: "")             # the backup copy
    monkeypatch.setattr(networks, "run", lambda *cmd, **kw: ran.append(cmd) or "")
    monkeypatch.setattr(networks, "is_sdn", lambda n: True)
    monkeypatch.setattr(networks, "bridge", lambda n: f"lab{n}")
    networks.ensure_gw_mac("95")
    networks.ensure_gw_mac("95")
    text = f.read_text()
    assert text.count("hwaddress") == 1
    assert "        address 10.10.95.1/24\n        hwaddress 02:00:0a:0a:5f:01\n" in text
    assert ran == [("ip", "link", "set", "lab95", "address", "02:00:0a:0a:5f:01")]


def test_ensure_gw_mac_leaves_bridge_nets(monkeypatch):
    monkeypatch.setattr(networks, "is_sdn", lambda n: False)
    monkeypatch.setattr(networks, "run", lambda *cmd, **kw: pytest.fail("vmbrN is never touched"))
    networks.ensure_gw_mac("30")


def test_new_sdn_stanza_has_the_gateway_mac(empty_pve):
    assert "hwaddress" not in networks.bridge_stanza("lab95", "10.10.95", [], sdn_made=True)    # one host: as before
    cluster(empty_pve)
    assert "hwaddress 02:00:0a:0a:5f:01" in networks.bridge_stanza("lab95", "10.10.95", [], sdn_made=True)
    assert "hwaddress" not in networks.bridge_stanza("vmbr95", "10.10.95", [])


def test_sdn_peers_from_members(empty_pve):
    cluster(empty_pve)
    assert sdn.node_ips() == ["10.0.0.123", "10.0.0.229"]
