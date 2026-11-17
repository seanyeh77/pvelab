"""A net's own iptables rules (its block's post-up lines): one that failed when the bridge came up is
found, and all of them are put back in order (a rule added alone would land after the net's DROPs)."""
import pytest

from pvelab import config, networks
from pvelab.ifaces import iptables, post_lines, section_marks
from pvelab.util import LabError

RULES = ["-t nat POSTROUTING -s 10.10.3.0/24 -o vmbr0 -j MASQUERADE",
         "FORWARD -i lab3 -o lab3 -j ACCEPT",
         "FORWARD -i lab3 -o vmbr0 -j ACCEPT",
         "FORWARD -i lab3 -j DROP"]


def write_block(rules=RULES, section=True):
    up, down = iptables(rules)
    block = "# lab.sh net 3 begin\niface lab3 inet static\n" + post_lines(["sysctl -qw net.ipv4.ip_forward=1"] + up, down)
    if section:                              # isolation's rule: its own section, not the net's own rules
        begin, end = section_marks("isolate", 3)
        block += begin + post_lines(*iptables(["-t mangle FORWARD -i lab3 -o lab3 -j DROP"], add="-I")) + end
    open(config.IFACES, "w").write(block + "# lab.sh net 3 end\n")


def fake_iptables(monkeypatch, have):
    """have: the rules the kernel holds now ("[-t T ]CHAIN ..."); ran: the shell lines run."""
    ran = []

    def rule_of(cmd):
        cmd = cmd.split(" 2>/dev/null")[0].replace("iptables -w 10 ", "iptables ")
        for op in ("-C", "-A", "-D", "-I"):
            if f" {op} " in cmd:
                head, rest = cmd.split(f" {op} ", 1)
                return op, (head[len("iptables "):] + " " if head != "iptables" else "") + rest
        raise AssertionError(cmd)

    def sh(*cmd, **kw):
        assert cmd[:2] == ("sh", "-c"), cmd
        ran.append(cmd[2])
        for part in cmd[2].split(" || "):
            op, rule = rule_of(part)
            if op == "-C":
                if rule in have:
                    return True
                continue
            if op == "-D":
                if rule not in have:
                    return False
                have.remove(rule)
            else:
                have.append(rule)
            return True
        return False
    monkeypatch.setattr(networks, "ok", sh)
    monkeypatch.setattr(networks.nets, "net_name", lambda n: "t97")
    monkeypatch.setattr(networks, "bridge", lambda n: "lab3")
    monkeypatch.setattr(networks, "run", lambda *c, **kw: sh(*c, **kw) or "")
    return ran


def test_rules_wait_for_the_xtables_lock():
    up, down = iptables(["FORWARD -i lab3 -j DROP"])
    assert up == ["iptables -w 10 -C FORWARD -i lab3 -j DROP 2>/dev/null || iptables -w 10 -A FORWARD -i lab3 -j DROP"]
    assert down == ["iptables -w 10 -D FORWARD -i lab3 -j DROP"]


def test_missing_rules_names_the_nets_own_rules_not_there(monkeypatch):
    write_block()
    fake_iptables(monkeypatch, [r for r in RULES if "-o vmbr0 -j ACCEPT" not in r])
    assert networks.missing_rules("3") == ["FORWARD -i lab3 -o vmbr0 -j ACCEPT"]


def test_no_block_here_no_rules_to_miss(monkeypatch):
    fake_iptables(monkeypatch, [])
    assert networks.missing_rules("3") == []


def test_repair_puts_all_back_in_order(monkeypatch):
    write_block()
    have = ["-t mangle FORWARD -i lab3 -o lab3 -j DROP"] + [r for r in RULES if "-o vmbr0 -j ACCEPT" not in r]
    fake_iptables(monkeypatch, have)
    said = []
    monkeypatch.setattr(networks, "warn", said.append)
    assert networks.repair_rules("3") == ["FORWARD -i lab3 -o vmbr0 -j ACCEPT"]
    assert have == ["-t mangle FORWARD -i lab3 -o lab3 -j DROP"] + RULES      # the section's rule untouched
    assert "FORWARD -i lab3 -o vmbr0 -j ACCEPT" in said[0] and "put back in order" in said[0]


def test_repair_with_every_rule_there_changes_nothing(monkeypatch):
    write_block()
    ran = fake_iptables(monkeypatch, list(RULES))
    assert networks.repair_rules("3") == []
    assert not [c for c in ran if " -D " in c or " -A " in c.split(" || ")[0]]


def test_old_rules_without_w_are_checked_too(monkeypatch):
    up, down = iptables(RULES)
    up, down = [u.replace("iptables -w 10 ", "iptables ") for u in up], [d.replace("iptables -w 10 ", "iptables ") for d in down]
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n" + post_lines(up, down) + "# lab.sh net 3 end\n")
    fake_iptables(monkeypatch, RULES[:1])
    assert networks.missing_rules("3") == RULES[1:]


def test_ensure_bridge_repairs_an_existing_net(monkeypatch):
    write_block()
    monkeypatch.setattr(networks, "bridge", lambda n: "lab3")
    monkeypatch.setattr(networks, "link_exists", lambda d: True)
    monkeypatch.setattr(networks.nets, "cidr", lambda n: __import__("ipaddress").ip_network("10.10.3.0/24"))
    got = []
    monkeypatch.setattr(networks, "repair_rules", lambda n: got.append(n))
    networks.ensure_bridge("3")
    assert got == ["3"]


def net_check(monkeypatch, gone, args=("check", "t97")):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd, "check_bridge", lambda v: ("3", "lab3", ("10.10.3.250", 24, "10.10.3.1", ["10.10.3.1"])))
    monkeypatch.setattr(netcmd, "missing_rules", lambda n: gone)
    monkeypatch.setattr(netcmd, "bridge_is_ours", lambda n: True)
    monkeypatch.setattr(netcmd.nets, "net_name", lambda n: "t97")
    monkeypatch.setattr(netcmd.netcheck, "check", lambda br, mirror, static=None: [("address", True, "x"),
                                                                                   ("packages", True, "200")])
    return netcmd.cmd_net_check(list(args))


def test_net_check_says_which_rules_are_missing(monkeypatch, capsys):
    with pytest.raises(LabError, match=r"pvelab net fix t97"):
        net_check(monkeypatch, ["FORWARD -i lab3 -o vmbr0 -j ACCEPT"])
    out = capsys.readouterr().out
    assert "rules" in out and "FORWARD -i lab3 -o vmbr0 -j ACCEPT" in out


def test_net_check_with_its_rules_in_place(monkeypatch, capsys):
    net_check(monkeypatch, [])
    out = capsys.readouterr().out
    assert "rules" in out and "gets a network" in out


def test_net_fix_puts_the_rules_back(monkeypatch, capsys):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd.nets, "resolve", lambda v: "3")
    monkeypatch.setattr(netcmd, "bridge_is_ours", lambda n: True)
    monkeypatch.setattr(netcmd, "repair_rules", lambda n: [])
    netcmd.cmd_net_fix(["fix", "t97"])
    assert "in place" in capsys.readouterr().out


def test_net_fix_refuses_a_net_that_is_not_pvelabs_here(monkeypatch):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd.nets, "resolve", lambda v: "vmbr0")
    monkeypatch.setattr(netcmd, "bridge_is_ours", lambda n: False)
    with pytest.raises(LabError, match="not pvelab's"):
        netcmd.cmd_net_fix(["fix", "vmbr0"])
