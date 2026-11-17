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


@pytest.fixture(autouse=True)
def forwarding_on(tmp_path, monkeypatch):
    f = tmp_path / "ip_forward"
    f.write_text("1\n")
    monkeypatch.setattr(networks, "IP_FORWARD", str(f))
    return f


def write_block(rules=RULES, section=True):
    up, down = iptables(rules)
    block = "# lab.sh net 3 begin\niface lab3 inet static\n" + post_lines(["sysctl -qw net.ipv4.ip_forward=1"] + up, down)
    if section:                              # isolation's rule: its own section, not the net's own rules
        begin, end = section_marks("isolate", 3)
        block += begin + post_lines(*iptables(["-t mangle FORWARD -i lab3 -o lab3 -j DROP"], add="-I")) + end
    open(config.IFACES, "w").write(block + "# lab.sh net 3 end\n")


def fake_iptables(monkeypatch, have, fail=()):
    """have: the rules the kernel holds now ("[-t T ]CHAIN ..."), in order; ran: the commands run (the
    -C checks as shell lines, iptables-restore as its input's lines); fail: rules whose adding fails."""
    ran = []

    def check(rule):
        ran.append(f"check {rule}")
        return rule in have

    def apply(cmds, kept):
        for rule, op in cmds:
            if op == "-D":
                if rule not in kept:
                    raise LabError("iptables-restore: Bad rule (does a matching rule exist in that chain?)")
                kept.remove(rule)            # the first copy, as iptables -D
            elif rule in fail:
                raise LabError(f"iptables-restore: line failed: {rule}")
            elif op == "-I":
                kept.insert(0, rule)
            else:
                kept.append(rule)

    def run(*cmd, data=None, **kw):
        if cmd[0] == "sysctl":
            ran.append(" ".join(cmd))
            open(networks.IP_FORWARD, "w").write("1\n")
            return ""
        assert cmd[0] == "iptables-restore" and "--noflush" in cmd, cmd
        table, cmds = None, []
        for line in data.splitlines():
            ran.append(line)
            if line.startswith("*"):
                table, cmds = line[1:], []
            elif line == "COMMIT":           # one table: whole or not at all
                kept = list(have)
                apply(cmds, kept)
                have[:] = kept
            else:
                op, rest = line.split(" ", 1)
                cmds.append(((f"-t {table} " if table != "filter" else "") + rest, op))
        return ""
    monkeypatch.setattr(networks, "has_rule", check)
    monkeypatch.setattr(networks, "run", run)
    monkeypatch.setattr(networks.nets, "net_name", lambda n: "t97")
    monkeypatch.setattr(networks, "bridge", lambda n: "lab3")
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


def test_repair_keeps_the_drop_rules_throughout(monkeypatch):
    write_block()
    ran = fake_iptables(monkeypatch, [r for r in RULES if "-o vmbr0 -j ACCEPT" not in r])
    monkeypatch.setattr(networks, "warn", lambda m: None)
    networks.repair_rules("3")
    assert ran[-5:] == ["*filter", "-A FORWARD -i lab3 -o vmbr0 -j ACCEPT", "-A FORWARD -i lab3 -j DROP",
                        "-D FORWARD -i lab3 -j DROP", "COMMIT"]      # one commit: the new copy, the older one gone
    assert not [c for c in ran if "MASQUERADE" in c and not c.startswith("check ")]      # another chain: left alone


def test_a_rule_that_cannot_be_added_leaves_the_others(monkeypatch):
    write_block()
    have = [r for r in RULES if "-o vmbr0 -j ACCEPT" not in r]
    fake_iptables(monkeypatch, have, fail={"FORWARD -i lab3 -o vmbr0 -j ACCEPT"})
    with pytest.raises(LabError, match="putting them back failed .*as it was"):
        networks.repair_rules("3")
    assert have == [r for r in RULES if "-o vmbr0 -j ACCEPT" not in r]


def test_older_plain_rules_count_too(monkeypatch):
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n"
                                   "        post-up   iptables -t nat -A POSTROUTING -s 10.10.3.0/24 -o vmbr0 -j MASQUERADE\n"
                                   "        post-down iptables -t nat -D POSTROUTING -s 10.10.3.0/24 -o vmbr0 -j MASQUERADE\n"
                                   "# lab.sh net 3 end\n")
    fake_iptables(monkeypatch, [])
    assert networks.missing_rules("3") == ["-t nat POSTROUTING -s 10.10.3.0/24 -o vmbr0 -j MASQUERADE"]


def test_forwarding_off_is_missing_and_turned_on(monkeypatch, forwarding_on):
    write_block()
    forwarding_on.write_text("0\n")
    ran = fake_iptables(monkeypatch, list(RULES))
    monkeypatch.setattr(networks, "warn", lambda m: None)
    assert networks.repair_rules("3") == ["net.ipv4.ip_forward=1"]
    assert forwarding_on.read_text() == "1\n" and "sysctl -qw net.ipv4.ip_forward=1" in ran
    assert "COMMIT" not in ran               # the rules were all there


def test_repair_gives_older_lines_w(monkeypatch):
    up, down = iptables(RULES)
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n" + post_lines([u.replace("-w 10 ", "") for u in up], down)
                                   + "# lab.sh net 3 end\n")
    fake_iptables(monkeypatch, list(RULES))
    networks.repair_rules("3")
    assert open(config.IFACES).read().count("iptables -w 10 ") == 3 * len(RULES)


def test_a_bare_w_line_is_a_rule_too(monkeypatch):
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n        post-up   iptables -w -A FORWARD -i lab3 -j DROP\n"
                                   "# lab.sh net 3 end\n")
    from pvelab.ifaces import own_rules
    assert own_rules("3") == [("FORWARD -i lab3 -j DROP", "-A")]


def test_a_new_sdn_net_missing_a_rule_is_no_bridge_instead(monkeypatch):
    monkeypatch.setattr(networks, "bridge", lambda n: "lab3")
    monkeypatch.setattr(networks.nets, "cidr", lambda n: __import__("ipaddress").ip_network("10.10.3.0/24"))
    monkeypatch.setattr(networks, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(networks, "is_sdn", lambda n: True)
    monkeypatch.setattr(networks, "check_free", lambda *a: None)
    monkeypatch.setattr(networks, "make_sdn_net", lambda *a: None)
    monkeypatch.setattr(networks, "reload_done", lambda: None)
    monkeypatch.setattr(networks, "repair_rules", lambda n: networks.die("t97: putting them back failed"))
    monkeypatch.setattr(networks, "add_net_block", lambda *a: pytest.fail("a bridge made instead"))
    said = []
    monkeypatch.setattr(networks, "warn", said.append)
    networks.ensure_bridge("3")              # on to the net's isolation: said, not stopped at
    assert said == ["t97: putting them back failed"]


def test_a_check_iptables_cannot_answer_is_no_missing_rule(monkeypatch):
    import subprocess
    monkeypatch.setattr(networks.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 4, "", "Another app is currently holding the xtables lock."))
    with pytest.raises(LabError, match="could not check FORWARD -i lab3 -j DROP: Another app"):
        networks.has_rule("FORWARD -i lab3 -j DROP")


def test_a_missing_insert_rule_moves_nothing(monkeypatch):
    up, down = iptables(["FORWARD -i vmbr0 -o lab3 -j ACCEPT"], add="-I")
    up2, down2 = iptables(RULES[1:])
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n" + post_lines(up + up2, down + down2) + "# lab.sh net 3 end\n")
    have = list(RULES[1:])
    ran = fake_iptables(monkeypatch, have)
    monkeypatch.setattr(networks, "warn", lambda m: None)
    networks.repair_rules("3")
    assert have == ["FORWARD -i vmbr0 -o lab3 -j ACCEPT"] + RULES[1:]
    assert ran[-3:] == ["*filter", "-I FORWARD -i vmbr0 -o lab3 -j ACCEPT", "COMMIT"]


def test_older_lines_get_w_before_a_reload(monkeypatch):
    from pvelab import ifaces
    up, down = iptables(RULES[:1])
    old = post_lines([u.replace("iptables -w 10 ", "iptables ") for u in up], [d.replace("iptables -w 10 ", "iptables ") for d in down])
    open(config.IFACES, "w").write(old + "        post-up   nft -f x\n")
    monkeypatch.setattr(ifaces, "run", lambda *a, **k: "")
    ifaces.make_rules_add_once()
    assert open(config.IFACES).read() == post_lines(up, down) + "        post-up   nft -f x\n"
    ifaces.make_rules_add_once()             # once only
    assert open(config.IFACES).read().count("-w 10") == 3


def test_repair_with_every_rule_there_changes_nothing(monkeypatch):
    write_block()
    ran = fake_iptables(monkeypatch, list(RULES))
    assert networks.repair_rules("3") == []
    assert not [c for c in ran if not c.startswith("check ")]       # the checks alone


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
