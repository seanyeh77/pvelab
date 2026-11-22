"""pvelab forward: ports (TCP, UDP, a range) or ping of this host forwarded to one VM, kept in its record and
in its net's forward section. No host commands: the section's shell and ss are faked."""
import pathlib
import types

import pytest

from pvelab import config, forwards, ifaces, networks, nodes, registry, ui
from pvelab.commands import complete, forward, listing
from pvelab.forwards import Forward
from pvelab.helptext import UsageError
from pvelab.util import LabError


def net_block(n, addr, name):
    return (f"# lab.sh net {n} begin (managed by lab)\n        # pvelab name {name}\n"
            f"iface vmbr{n} inet static\n        address {addr}\n# lab.sh net {n} end\n")


def record(vmid, name, net="30", ip="10.30.0.11", **fields):
    d = pathlib.Path(nodes.NODES_DIR) / nodes.this_node() / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(f"name: {name}\nnet0: virtio=BC:24:11:00:00:{vmid % 100:02d},bridge=vmbr{net}\n"
                                    f"smbios1: uuid=1111-{vmid}\n")
    registry.update({vmid: {"name": name, "net": net, "ip": ip, "user": "student", "password": "pw", **fields}},
                    {vmid: {"smbios1": f"uuid=1111-{vmid}"}})


@pytest.fixture(autouse=True)
def lab(monkeypatch):
    """Net 30 prod (its gateway here); the section's shell lines kept in `ran`; nothing listens."""
    with open(config.IFACES, "w") as f:
        f.write(net_block("30", "10.30.0.1/24", "prod"))
    ran = []
    monkeypatch.setattr(ifaces, "run", lambda *cmd, **k: ran.append(cmd[-1]) or "")
    monkeypatch.setattr(ifaces, "subprocess", types.SimpleNamespace(run=lambda *a, **k: None, DEVNULL=None))
    monkeypatch.setattr(forward, "listening_ports", lambda proto: set())
    return ran


def section():
    m = ifaces.section_re("forward", "30").search(open(config.IFACES).read())
    return m.group(0) if m else ""


# --- what a forward is ---

@pytest.mark.parametrize("spec,want,text", [
    ("51820/udp", Forward("udp", 51820, 51820, 51820), "51820/udp"),
    ("80", Forward("tcp", 80, 80, 80), "80/tcp"),
    ("8080:80", Forward("tcp", 8080, 8080, 80), "8080:80/tcp"),
    ("6000-6010/UDP", Forward("udp", 6000, 6010, 6000), "6000-6010/udp"),
    ("ICMP", Forward("icmp", None, None, None), "icmp"),
])
def test_parse_and_canonical(spec, want, text):
    assert forwards.parse(spec) == want and forwards.canonical(want) == text
    assert forwards.parse(text) == want


@pytest.mark.parametrize("spec,why", [("0", "1-65535"), ("70000", "1-65535"), ("10-5", "backwards"),
                                      ("80/sctp", "not sctp"), ("http", "no forward"), ("1-2:3", "no forward")])
def test_parse_says_what_is_wrong(spec, why):
    with pytest.raises(UsageError, match=why):
        forwards.parse(spec)


def test_overlaps():
    p = forwards.parse
    assert forwards.overlaps(p("6000-6010/udp"), p("6005/udp"))
    assert not forwards.overlaps(p("6000-6010/udp"), p("6005/tcp"))
    assert not forwards.overlaps(p("8080:80"), p("80"))          # this host's ports count, not the VM's
    assert forwards.overlaps(p("icmp"), p("icmp"))


def test_rules_only_let_dnat_in():
    fwd, nat = forwards.rules("30", "10.30.0.11", forwards.parse("8080:80"))
    assert fwd == ("FORWARD -i vmbr0 -p tcp -o vmbr30 -d 10.30.0.11 --dport 80 -m conntrack --ctstate DNAT "
                   "-j ACCEPT")
    assert nat == ("-t nat PREROUTING -i vmbr0 -p tcp -m addrtype --dst-type LOCAL --dport 8080 -j DNAT "
                   "--to-destination 10.30.0.11:80")
    fwd, nat = forwards.rules("30", "10.30.0.11", forwards.parse("6000-6010/udp"))
    assert "--dport 6000:6010" in fwd and nat.endswith("--dport 6000:6010 -j DNAT --to-destination 10.30.0.11")
    fwd, nat = forwards.rules("30", "10.30.0.11", forwards.parse("icmp"))
    assert nat == ("-t nat PREROUTING -i vmbr0 -p icmp -m addrtype --dst-type LOCAL --icmp-type echo-request "
                   "-j DNAT --to-destination 10.30.0.11")


# --- the net's section ---

def test_set_forwards_writes_the_records_forwards(lab):
    record(100, "wg", forwards=["51820/udp", "icmp"])
    record(101, "other", net="31", ip="10.31.0.11", forwards=["80/tcp"])        # another net: not here
    forwards.set_forwards("30")
    s = section()
    assert "--dport 51820 -j DNAT --to-destination 10.30.0.11:51820" in s
    assert "--icmp-type echo-request" in s and "10.31.0.11" not in s
    assert "post-down iptables -w 10 -t nat -D PREROUTING" in s
    assert lab and "iptables -w 10 -I FORWARD -i vmbr0 -p udp" in lab[-1]       # applied now; ACCEPT first


def test_no_forwards_no_section_and_no_write(lab):
    before = open(config.IFACES).read()
    forwards.set_forwards("30")
    assert open(config.IFACES).read() == before and not lab


def test_the_section_follows_the_vm(lab):
    record(100, "wg", forwards=["51820/udp"])
    networks.refresh_net("30")
    assert "10.30.0.11:51820" in section()
    registry.update({100: {"ip": "10.30.0.20"}}, {100: {"smbios1": "uuid=1111-100"}})     # a new address
    networks.refresh_net("30")
    assert "10.30.0.20:51820" in section() and "10.30.0.11" not in section()
    registry.forget([100])                                                               # destroyed
    networks.refresh_net("30")
    assert section() == ""


def test_a_service_started_later_does_not_drop_the_forward(monkeypatch):
    record(100, "wg", forwards=["30000-40000/udp"])
    monkeypatch.setattr(forward, "listening_ports", lambda proto: {35000})    # refresh does not look (ss)
    forwards.set_forwards("30")
    assert "--dport 30000:40000" in section()


def test_expose_does_not_take_a_forwarded_port(monkeypatch):
    from pvelab import expose
    record(100, "wg", forwards=["22101/tcp"])
    monkeypatch.setattr(expose, "listening_ports", lambda: set())
    assert expose.busy_ports() == {22101}


# --- pvelab forward ---

def test_forward_adds_and_removes(lab, capsys):
    record(100, "wg")
    forward.cmd_forward(["wg", "51820/udp"])
    out = capsys.readouterr().out
    assert "forward UDP 51820 arriving on this host (vmbr0) to wg (10.30.0.11:51820)" in out
    assert "wg: 51820/udp forwarded. Allow UDP 51820 in the cloud security group" in out
    assert registry.load()["vms"]["100"]["forwards"] == ["51820/udp"]
    assert "10.30.0.11:51820" in section()
    forward.cmd_forward(["wg", "51820/udp"])
    assert "wg forwards 51820/udp already" in capsys.readouterr().out
    forward.cmd_forward(["wg", "--off"])
    assert "wg: 51820/udp no longer forwarded" in capsys.readouterr().out
    assert "forwards" not in registry.load()["vms"]["100"] and section() == ""


def test_dry_run_changes_nothing(capsys):
    record(100, "wg")
    forward.cmd_forward(["wg", "51820/udp"], dry=True)
    assert "dry run - nothing was changed" in capsys.readouterr().out
    assert "forwards" not in registry.load()["vms"]["100"] and section() == ""


def test_remove_one_that_is_not_there():
    record(100, "wg", forwards=["51820/udp"])
    with pytest.raises(LabError, match=r"wg does not forward 80/tcp \(its forwards: 51820/udp\)"):
        forward.cmd_forward(["wg", "80", "--off"])


@pytest.mark.parametrize("spec,why", [
    ("6005/udp", "6005/udp: web has 6000-6010/udp already"),
    ("icmp", "icmp: ping goes to web already"),
    ("22101", "22101/tcp: TCP 22101 is web's ssh port"),
])
def test_conflicts_are_refused(monkeypatch, spec, why):
    record(101, "web", forwards=["6000-6010/udp", "icmp"])
    record(100, "wg")
    monkeypatch.setattr(forward, "exposure", lambda: ({"30"}, {101}))
    with pytest.raises(LabError, match=why):
        forward.cmd_forward(["wg", spec])


def test_forwards_on_another_hosts_uplink_do_not_clash(monkeypatch):
    from pvelab import expose
    record(101, "web2", net="31", ip="10.31.0.11", forwards=["443/tcp", "icmp"])    # net31: gateway elsewhere
    record(100, "wg")
    forward.cmd_forward(["wg", "443", "icmp"], dry=True)
    monkeypatch.setattr(expose, "listening_ports", lambda: set())
    assert expose.busy_ports() == set()


def test_two_new_ones_that_overlap_are_refused():
    record(100, "wg")
    with pytest.raises(LabError, match="7080/tcp: wg has 7000-7090/tcp already"):
        forward.cmd_forward(["wg", "7000-7090", "7080"])


def test_a_port_a_service_listens_on_is_refused(monkeypatch):
    record(100, "wg")
    monkeypatch.setattr(forward, "listening_ports", lambda proto: {2222} if proto == "tcp" else set())
    with pytest.raises(LabError, match="2222/tcp: TCP 2222: a service on this host listens there"):
        forward.cmd_forward(["wg", "2222"])


@pytest.mark.parametrize("spec,why", [("22", "TCP 22: this host's own"), ("60010", "60000-60050 \\(live migration\\)"), ("8000-8010", "TCP 8006: this host's own"),
                                      ("4789/udp", r"UDP 4789 \(VXLAN\): this host's own"), ("5405/udp", "UDP 5404-5412"),
                                      ("22150", r"TCP 22100-31999 \(pvelab expose's ssh ports\)")])
def test_this_hosts_own_ports_are_never_forwarded(spec, why):
    record(100, "wg")
    with pytest.raises(LabError, match=why):
        forward.cmd_forward(["wg", spec])


def test_a_vm_blocked_from_the_internet_is_warned_about(monkeypatch, capsys):
    record(100, "wg", blocks=["internet"])
    monkeypatch.setattr(ui, "YES", True)
    forward.cmd_forward(["wg", "51820/udp"])
    assert ("wg is blocked from the internet: what its forwards bring in gets no answer until pvelab unblock "
            "internet wg") in capsys.readouterr().out


def test_block_internet_drops_forwarded_traffic_but_not_exposed_ssh(monkeypatch):
    from pvelab import blocks
    record(100, "wg", forwards=["51820/udp"])
    monkeypatch.setattr(blocks, "vm_mac", lambda v: None)
    rules = blocks.block_rules("30", {100: {"internet"}}, {100: {"net": "30", "ip": "10.30.0.11"}})
    assert rules == ["FORWARD -i vmbr30 -o vmbr0 -s 10.30.0.11 -m conntrack ! --ctstate DNAT -j DROP",
                     "FORWARD -i vmbr30 -o vmbr0 -s 10.30.0.11 -m conntrack --ctstate DNAT ! --ctorigdstport 22100 "
                     "-j DROP"]


def test_a_record_without_a_net_is_no_pvelab_net():
    record(100, "wg", net=None, ip=None)
    with pytest.raises(LabError, match="wg is on no pvelab net"):
        forward.cmd_forward(["wg", "80"])


def test_ping_to_a_vm_warns_that_this_host_stops_answering(monkeypatch, capsys):
    record(100, "wg")
    monkeypatch.setattr(ui, "YES", True)
    forward.cmd_forward(["wg", "icmp"])
    out = capsys.readouterr().out
    assert ("this host stops answering ping on vmbr0 - from the internet and from its LAN too (the other PVE hosts, "
            "monitoring): wg answers it, and nothing does while wg is off") in out
    assert "Allow ping (ICMP echo) in the cloud security group" in out


def test_a_net_whose_gateway_is_elsewhere_is_refused():
    record(100, "wg", net="31", ip="10.31.0.11")
    with pytest.raises(LabError, match="net31 has no lab bridge"):
        forward.cmd_forward(["wg", "80"])


def test_off_on_a_net_that_is_gone_only_changes_the_record(capsys):
    record(100, "wg", net="31", ip="10.31.0.11", forwards=["80/tcp"])            # no block for net31 here
    forward.cmd_forward(["wg", "--off"])
    assert "forwards" not in registry.load()["vms"]["100"]


def test_off_of_a_spec_when_there_are_none_fails():
    record(100, "wg")
    with pytest.raises(LabError, match=r"wg does not forward 80/tcp \(it has none\)"):
        forward.cmd_forward(["wg", "80", "--off"])


def test_a_record_whose_forwards_is_no_list(capsys):
    record(100, "wg", forwards="51820/udp")
    assert forwards.recorded(say=True) == {}
    assert capsys.readouterr().err.count("forwards is no list") == 1


def test_listing(capsys):
    record(100, "wg", forwards=["51820/udp"])
    record(101, "web")
    forward.cmd_forward([])
    assert capsys.readouterr().out == "wg                 51820/udp        -> 10.30.0.11:51820\n"
    forward.cmd_forward(["web"])
    assert "web has no port forwards" in capsys.readouterr().out
    registry.update({100: {"ip": None}}, {100: {"smbios1": "uuid=1111-100"}})
    forward.cmd_forward([])
    assert "-> -:51820" in capsys.readouterr().out


def test_listening_ports_only_counts_what_the_uplink_reaches(monkeypatch):
    from pvelab import util
    ss = ["LISTEN 0 128 0.0.0.0:22 0.0.0.0:*", "LISTEN 0 128 [::]:8006 [::]:*", "LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*",
          "UNCONN 0 0 10.30.0.1:53 0.0.0.0:*", "UNCONN 0 0 0.0.0.0%lab2:67 0.0.0.0:*", "LISTEN 0 128 192.0.2.1:3128 0:*",
          "LISTEN 0 128 192.0.2.9:443 0:*", "LISTEN 0 128 *:9000 *:*"]
    out = {"ss": "\n".join(ss), "ip": "5: vmbr0    inet 192.0.2.1/24 brd x\n5: vmbr0    inet 192.0.2.9/24 brd x\n",
           "wg": "wg0\t51820\nwg1\t0\n"}
    monkeypatch.setattr(util, "run", lambda *cmd, **k: "" if cmd[:2] == ("ss", "-Hlun") else out[cmd[0]])
    monkeypatch.setattr(util.shutil, "which", lambda prog: "/usr/bin/" + prog)
    assert util.listening_ports() == {22, 8006, 3128, 443, 9000}       # a lab bridge's, loopback's: not
    assert util.listening_ports("udp") == {51820}                      # the kernel's WireGuard: ss lists it not


def test_a_failed_apply_leaves_the_record_as_it_was(monkeypatch):
    record(100, "wg", forwards=["80/tcp"])
    monkeypatch.setattr(forward, "set_forwards", lambda n, recs: (_ for _ in ()).throw(LabError("iptables failed")))
    with pytest.raises(LabError, match="iptables failed"):
        forward.cmd_forward(["wg", "51820/udp"])
    assert registry.load()["vms"]["100"]["forwards"] == ["80/tcp"]


def test_a_broken_record_is_said_where_people_look_only(capsys):
    record(100, "wg", forwards=["80/sctp"])
    forwards.recorded()
    forwards.set_forwards("30")
    assert "left out" not in capsys.readouterr().err
    forward.cmd_forward([])
    assert capsys.readouterr().err.count("80/sctp': tcp, udp or icmp, not sctp - left out") == 1
@pytest.mark.parametrize("args", [["wg", "http"], ["--off"], ["wg", "--udp"]])
def test_bad_usage(args):
    record(100, "wg")
    with pytest.raises(UsageError):
        forward.cmd_forward(args)


def test_completion():
    assert complete.candidates(["forward", "w"], 2, lambda k: ["wg", "web"]) == ["wg", "web"]
    assert complete.candidates(["forward", "wg", ""], 3, lambda k: ["wg"]) == ["--off", "--dry-run", "icmp"]


def test_list_names_the_forwards(capsys):
    record(100, "wg", forwards=["51820/udp"])
    listing.summary({100: {"name": "wg", "_rec": {"forwards": ["51820/udp"]}}}, {}, {}, (5, 90), set(), set(), True)
    assert "port forwards: wg 51820/udp  (pvelab forward)" in capsys.readouterr().out


def test_the_same_section_again_takes_nothing_away(lab, monkeypatch):
    record(100, "wg", forwards=["51820/udp"])
    forwards.set_forwards("30")
    downs = []
    monkeypatch.setattr(ifaces, "subprocess", types.SimpleNamespace(run=lambda *a, **k: downs.append(a), DEVNULL=None))
    monkeypatch.setattr(ifaces, "save_ifaces", lambda text: downs.append("saved"))
    lab.clear()
    forwards.set_forwards("30")                  # another VM came or went: wg's rules stay as they are
    assert downs == [] and len(lab) == 1 and "51820" in lab[0]    # only added again where missing
