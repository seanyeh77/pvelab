"""pvelab list --json: a net's VMs for scripts, built field by field (never a password or key). No host
commands: qm list (vm_statuses) is faked, the configs and records are in empty_pve."""
import json
import pathlib

import pytest

from pvelab import config, nodes, registry
from pvelab.commands import listing
from pvelab.helptext import UsageError

HERE = nodes.this_node()
CA = pathlib.Path(__file__).with_name("test-ca.pem")
CA_SHA256 = ("0D:D0:44:34:63:B5:B1:A3:35:21:8B:0E:4A:F9:C7:3C:AC:B4:64:5A:5F:8D:4F:5A:2E:6F:38:BC:40:62:EB:6B")
SECRET, KEY = "Pw-marker-7f3a", "ssh-ed25519 AAAAC3marker-key"


def net_block(n, name, expose=()):
    """Net n's block in the interfaces file; expose: the VMIDs its expose section forwards."""
    sec = ""
    if expose:
        sec = (f"        # expose net {n} begin\n"
               + "".join(f"        post-up iptables -t nat -A PREROUTING -i vmbr0 -p tcp --dport {22000 + v} "
                         f"-j DNAT --to-destination 10.10.{n}.{10 + v % 100}:22\n" for v in expose)
               + f"        # expose net {n} end\n")
    return (f"# lab.sh net {n} begin (managed by lab)\n        # pvelab name {name}\n"
            f"iface vmbr{n} inet static\n        address 10.10.{n}.1/24\n{sec}# lab.sh net {n} end\n")


def record(vmid, name, net="40", ip=None, node=HERE, **fields):
    d = pathlib.Path(nodes.NODES_DIR) / node / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(f"name: {name}\nnet0: virtio=BC:24:11:00:00:{vmid % 100:02d},bridge=vmbr{net}\n"
                                    f"cipassword: {SECRET}\nsshkeys: {KEY.replace(' ', '%20')}\n"
                                    f"smbios1: uuid=1111-{vmid}\n")
    registry.update({vmid: {"name": name, "net": net, "ip": ip or f"10.10.{net}.{10 + vmid % 100}", "user": "student",
                            "password": SECRET, **fields}}, {vmid: {"smbios1": f"uuid=1111-{vmid}"}})


@pytest.fixture(autouse=True)
def lab(monkeypatch, empty_pve):
    """Net 40 demo here, exposed for 201 and 202; the CA, a pool, the saved --host."""
    (empty_pve / "interfaces").write_text(net_block("40", "demo", expose=(201, 202)))
    monkeypatch.setattr(config, "PVE_CA", str(CA))
    (empty_pve / "user.cfg").write_text("user:root@pam:1:0:::::\npool:pvelab-demo:demo net:201,202,999:local-lvm:\n"
                                        "pool:empty::::\n")
    monkeypatch.setattr(config, "USER_CFG", str(empty_pve / "user.cfg"))
    (empty_pve / "public-host").write_text("198.51.100.20\n")
    monkeypatch.setattr(config, "PUBLIC_HOST_FILE", str(empty_pve / "public-host"))
    monkeypatch.setattr(listing, "vm_statuses", lambda ids: {i: "running" for i in ids})
    return empty_pve


def listed(capsys, *args):
    listing.cmd_list(["--json", *args])
    return json.loads(capsys.readouterr().out)


def test_fields_in_their_order(capsys):
    record(201, "demo-alice-1", owner="alice")
    record(202, "demo-alice-2", owner="alice")
    record(203, "demo-bob", owner="bob")
    out = listed(capsys, "--net", "demo")
    assert list(out) == ["schema", "pvelab", "node", "public_host", "cluster", "nets", "warnings"]
    assert out["schema"] == 1 and out["node"] == HERE and out["public_host"] == "198.51.100.20"
    assert out["warnings"] == []
    net, = out["nets"]
    assert list(net) == ["name", "number", "cidr", "gateway", "bridge", "gateway_node", "internal_host", "isolated",
                         "exposed", "shared", "owners"]
    assert net | {"owners": None} == {"name": "demo", "number": "40", "cidr": "10.10.40.0/24", "gateway": "10.10.40.1",
                                      "bridge": "vmbr40", "gateway_node": HERE, "internal_host": "192.0.2.1",
                                      "isolated": False, "exposed": True, "shared": None, "owners": None}
    assert [o["owner"] for o in net["owners"]] == ["alice", "bob"]
    alice, bob = net["owners"]
    assert alice["node"] == HERE and list(alice) == ["owner", "node", "vms"]
    vm = alice["vms"][0]
    assert list(vm) == ["vmid", "name", "index", "node", "pool", "user", "ip", "status", "ssh_port"]
    assert vm == {"vmid": 201, "name": "demo-alice-1", "index": 1, "node": HERE, "pool": "pvelab-demo",
                  "user": "student", "ip": "10.10.40.11", "status": "running", "ssh_port": 22201}
    assert [v["index"] for v in alice["vms"]] == [1, 2] and alice["vms"][1]["ssh_port"] == 22202
    assert bob["vms"][0]["index"] == 1 and bob["vms"][0]["ssh_port"] is None     # not forwarded
    assert bob["vms"][0]["pool"] is None                                          # in no pool


def test_no_secrets(capsys):
    record(201, "demo-alice-1", owner="alice")
    listing.cmd_list(["--json"])
    text = capsys.readouterr().out
    assert SECRET not in text and "marker-key" not in text and "password" not in text
    json.loads(text)


@pytest.mark.parametrize("other", ["--passwords", "--ssh"])
def test_json_refuses_passwords_and_ssh(other):
    with pytest.raises(UsageError, match="--json"):
        listing.cmd_list(["--json", other])


def test_vms_ordered_by_index_then_vmid(capsys):
    record(205, "demo-alice-2", owner="alice")
    record(204, "demo-alice-10", owner="alice")
    record(203, "demo-alice-1", owner="alice")
    vms = listed(capsys)["nets"][0]["owners"][0]["vms"]
    assert [(v["index"], v["vmid"]) for v in vms] == [(1, 203), (2, 205), (10, 204)]


def test_index_null_without_a_suffix_among_several(capsys):
    record(201, "demo-alice-1", owner="alice")
    record(202, "web", owner="alice")
    vms = listed(capsys)["nets"][0]["owners"][0]["vms"]
    assert [(v["vmid"], v["index"]) for v in vms] == [(201, 1), (202, None)]


def test_owner_key_of_digits_is_no_index(capsys):
    record(201, "demo-1101001", owner="1101001")
    assert listed(capsys)["nets"][0]["owners"][0]["vms"][0]["index"] == 1


def test_gateway_on_another_host(capsys, lab):
    (lab / "interfaces").write_text("")                         # net 40's block is not here
    record(201, "net40-alice-1", owner="alice")
    out = listed(capsys)
    net = out["nets"][0]
    assert (net["gateway_node"], net["internal_host"], net["exposed"], net["isolated"]) == (None, None, None, None)
    assert net["owners"][0]["vms"][0]["ssh_port"] is None
    assert out["warnings"] == [{"kind": "gateway_unknown", "net": "net40"}]


def test_gateway_of_a_taken_over_vnet(monkeypatch, lab):
    (lab / "interfaces").write_text("")
    monkeypatch.setattr(config, "taken_entry", lambda n: ("vnet1", {"net": 40, "host": "pve2"}))
    assert listing.gateway_node("40") == "pve2"
    monkeypatch.setattr(config, "taken_entry", lambda n: None)
    assert listing.gateway_node("40") is None


def test_internal_host_members_before_vmbr0(monkeypatch, lab):
    (lab / ".members").write_text(json.dumps({"nodename": HERE, "nodelist": {
        HERE: {"id": 1, "online": 1, "ip": "192.0.2.10"}, "pve2": {"id": 2, "online": 1, "ip": "192.0.2.11"},
        "pve3": {"id": 3, "online": 0}}}))
    assert listing.internal_host(HERE) == "192.0.2.10"
    assert listing.internal_host("pve2") == "192.0.2.11"
    assert listing.internal_host("pve3") is None                # not this host: no vmbr0 to fall back on
    assert listing.internal_host(None) is None
    (lab / ".members").write_text(json.dumps({"nodename": HERE, "nodelist": {HERE: {"id": 1, "online": 1}}}))
    assert listing.internal_host(HERE) == "192.0.2.1"            # vmbr0's (conftest)


def test_ca(capsys, monkeypatch, lab):
    out = listed(capsys)
    assert out["cluster"] == {"ca_sha256": CA_SHA256, "ca_pem": CA.read_text()}
    monkeypatch.setattr(config, "PVE_CA", str(lab / "none.pem"))
    assert listed(capsys)["cluster"] == {"ca_sha256": None, "ca_pem": None}
    (lab / "bad.pem").write_text("-----BEGIN CERTIFICATE-----\nnot base64!\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(config, "PVE_CA", str(lab / "bad.pem"))
    assert listed(capsys)["cluster"]["ca_sha256"] is None


def test_no_public_host_saved(capsys, monkeypatch, lab):
    monkeypatch.setattr(config, "PUBLIC_HOST_FILE", str(lab / "nothing"))
    monkeypatch.setattr(listing, "public_host", lambda: pytest.fail("asked the outside"))
    assert listed(capsys)["public_host"] is None


def test_pools():
    assert listing.vm_pools("pool:a::201,202:local-lvm:\npool:b:x:303::\nuser:root@pam:1:0:::::\npool:c::::\n") == \
        {201: "a", 202: "a", 303: "b"}


def test_owner_on_two_hosts(capsys, lab):
    (lab / ".members").write_text(json.dumps({"nodename": HERE, "nodelist": {
        HERE: {"id": 1, "online": 1, "ip": "192.0.2.10"}, "pve2": {"id": 2, "online": 1, "ip": "192.0.2.11"}}}))
    record(201, "demo-alice-1", owner="alice")
    record(202, "demo-alice-2", owner="alice", node="pve2")
    out = listed(capsys)
    alice = out["nets"][0]["owners"][0]
    assert alice["node"] is None and [v["node"] for v in alice["vms"]] == [HERE, "pve2"]
    assert out["warnings"] == [{"kind": "owner_split", "net": "demo", "owner": "alice", "nodes": sorted([HERE, "pve2"])}]


def test_shared_vm_adopted_vm_and_empty_net(capsys, lab):
    (lab / "interfaces").write_text(net_block("40", "demo", expose=(201,)) + net_block("41", "empty"))
    record(201, "demo-shared", role="shared")
    record(202, "box", ip="-", owner="carol")                     # adopted, no address recorded
    out = listed(capsys)
    demo, empty = out["nets"]
    assert demo["shared"] == {"vmid": 201, "name": "demo-shared", "node": HERE, "pool": "pvelab-demo",
                              "user": "student", "ip": "10.10.40.11", "status": "running", "ssh_port": 22201}
    carol, = demo["owners"]
    assert carol["owner"] == "carol" and carol["vms"][0]["ip"] is None and carol["vms"][0]["index"] == 1
    assert empty["name"] == "empty" and empty["owners"] == [] and empty["shared"] is None and empty["exposed"] is False
    assert listed(capsys, "--net", "empty")["nets"][0]["name"] == "empty"


def test_exposed_only(capsys, lab):
    (lab / "interfaces").write_text(net_block("40", "demo", expose=(201,)) + net_block("41", "closed"))
    record(201, "demo-alice", owner="alice")
    record(301, "closed-bob", net="41", owner="bob")
    assert [n["name"] for n in listed(capsys, "--exposed")["nets"]] == ["demo"]


def test_table_unchanged_without_json(monkeypatch, capsys):
    monkeypatch.setattr(listing, "list_json", lambda *a: pytest.fail("json without --json"))
    monkeypatch.setattr(listing, "list_ssh", lambda *a: print("ssh lines"))
    listing.cmd_list(["--ssh"])
    assert capsys.readouterr().out == "ssh lines\n"
