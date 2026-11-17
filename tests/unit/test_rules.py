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

    def listed(table, chain):                # iptables -S of a chain: canon of each rule there, in order
        return [networks.canon(networks.split_table(r)[1].split()) for r in have
                if networks.chain_of(r) == (table, chain)]
    monkeypatch.setattr(networks, "listed", listed)
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
    assert networks.repair_rules("3") == ["missing FORWARD -i lab3 -o vmbr0 -j ACCEPT"]
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
    with pytest.raises(LabError, match="putting the rules back failed .*as it was"):
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
    assert networks.repair_rules("3") == ["net.ipv4.ip_forward was off"]
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
    monkeypatch.setattr(networks, "settled", lambda n: None)
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
    with pytest.raises(networks.Unknown, match=r"could not check FORWARD -i lab3 -j DROP \(Another app"):
        networks.has_rule("FORWARD -i lab3 -j DROP")


def test_repair_when_iptables_cannot_tell_says_so_and_does_nothing(monkeypatch):
    write_block()
    ran = fake_iptables(monkeypatch, [])
    monkeypatch.setattr(networks, "has_rule", lambda r: (_ for _ in ()).throw(networks.Unknown("could not check")))
    said = []
    monkeypatch.setattr(networks, "warn", said.append)
    assert networks.repair_rules("3") == [] and said[0].startswith("t97: could not check - nothing put back")
    assert "COMMIT" not in ran
    with pytest.raises(LabError, match="nothing put back"):           # net fix: it stops
        networks.repair_rules("3", strict=True)


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
    admin = "iface vmbr0 inet static\n        post-up   iptables -t nat -C POSTROUTING -j X 2>/dev/null || true\n"
    waits = "        post-up   iptables -t nat --wait -A POSTROUTING -j Y\n"     # a plain add that waits
    open(config.IFACES, "w").write(admin + "# lab.sh net 3 begin\n" + old + waits + "        post-up   nft -f x\n# lab.sh net 3 end\n")
    monkeypatch.setattr(ifaces, "run", lambda *a, **k: "")
    ifaces.make_rules_add_once()
    once = post_lines(iptables(["-t nat POSTROUTING -j Y"])[0], [])        # the add-once form
    want = admin + "# lab.sh net 3 begin\n" + post_lines(up, down) + once + "        post-up   nft -f x\n# lab.sh net 3 end\n"
    assert open(config.IFACES).read() == want          # an admin's stanza: not pvelab's to change
    ifaces.make_rules_add_once()             # once only
    assert open(config.IFACES).read() == want
    assert ifaces.own_rules("3")[-1] == ("-t nat POSTROUTING -j Y", "-A")


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
    monkeypatch.setattr(netcmd, "missing", lambda n: (gone, False))
    monkeypatch.setattr(netcmd, "own_rules", lambda n: [("FORWARD -i lab3 -j DROP", "-A")])
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
    asked = []
    monkeypatch.setattr(netcmd, "local_net", lambda v, verb: asked.append((v, verb)) or "3")
    monkeypatch.setattr(netcmd, "repair_rules", lambda n, strict=False: [] if strict else pytest.fail("not strict"))
    monkeypatch.setattr(netcmd, "own_rules", lambda n: [("FORWARD -i lab3 -j DROP", "-A")])
    monkeypatch.setattr(netcmd, "missing", lambda n, rules=None: ([], False))
    monkeypatch.setattr(netcmd.nets, "net_name", lambda n: "t97")
    netcmd.cmd_net_fix(["fix", "t97"])
    assert asked == [("t97", "fix")] and "all in place" in capsys.readouterr().out
    monkeypatch.setattr(netcmd, "missing", lambda n, rules=None: ([], True))
    netcmd.cmd_net_fix(["fix", "t97"])
    assert "order not checked" in capsys.readouterr().out
    monkeypatch.setattr(netcmd, "own_rules", lambda n: [])
    netcmd.cmd_net_fix(["fix", "t97"])
    assert "none of its rules in a form pvelab checks" in capsys.readouterr().out


def test_net_check_with_no_rule_to_check_says_so(monkeypatch, capsys):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd, "check_bridge", lambda v: ("3", "lab3", ("10.10.3.250", 24, "10.10.3.1", ["10.10.3.1"])))
    monkeypatch.setattr(netcmd, "own_rules", lambda n: [])
    monkeypatch.setattr(netcmd, "bridge_is_ours", lambda n: True)
    monkeypatch.setattr(netcmd.netcheck, "check", lambda br, mirror, static=None: [("packages", True, "200")])
    netcmd.cmd_net_check(["check", "t97"])
    assert "none in a form pvelab checks" in capsys.readouterr().out

def test_an_existing_net_that_cannot_get_its_rules_back_stops_create(monkeypatch):
    write_block()
    monkeypatch.setattr(networks, "bridge", lambda n: "lab3")
    monkeypatch.setattr(networks, "link_exists", lambda d: True)
    monkeypatch.setattr(networks.nets, "cidr", lambda n: __import__("ipaddress").ip_network("10.10.3.0/24"))
    monkeypatch.setattr(networks, "repair_rules", lambda n: networks.die("t97: putting them back failed"))
    with pytest.raises(LabError, match="putting them back failed"):
        networks.ensure_bridge("3")


def bring_up(monkeypatch, addr):
    import subprocess
    ran, said = [], []
    monkeypatch.setattr(networks.subprocess, "run", lambda c, **k: ran.append(tuple(c)) or subprocess.CompletedProcess(
        c, 1, "", "error: lab3: cmd 'iptables -w 10 -A FORWARD' failed\n"))       # ifup: exit 1
    monkeypatch.setattr(networks, "link_exists", lambda d: True)                     # ... the bridge is up
    monkeypatch.setattr(networks.nets, "pve_gateway", lambda n: False)
    monkeypatch.setattr(networks.nets, "gateway", lambda n: __import__("ipaddress").ip_address("10.10.3.1"))
    monkeypatch.setattr(networks, "run", lambda *c, **k: addr if c[:2] == ("ip", "-4") else pytest.fail(f"{c}"))
    monkeypatch.setattr(networks, "warn", said.append)
    networks.bring_up("3", "lab3")
    return ran, said


def test_ifup_that_worked_still_has_its_gateway_checked(monkeypatch):
    import subprocess
    monkeypatch.setattr(networks.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 0, "", ""))
    monkeypatch.setattr(networks.nets, "pve_gateway", lambda n: False)
    monkeypatch.setattr(networks.nets, "gateway", lambda n: __import__("ipaddress").ip_address("10.10.3.1"))
    monkeypatch.setattr(networks, "run", lambda *c, **k: "")         # ip addr: no address
    with pytest.raises(LabError, match="came up without its gateway"):
        networks.bring_up("3", "lab3")


def test_a_bridge_ifup_did_not_bring_up_stops_with_its_error(monkeypatch):
    import subprocess
    monkeypatch.setattr(networks.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 1, "", "error: lab3: bad\n"))
    monkeypatch.setattr(networks, "link_exists", lambda d: False)
    with pytest.raises(LabError, match="ifup lab3 failed: error: lab3: bad"):
        networks.bring_up("3", "lab3")


def test_a_post_up_line_that_failed_is_for_the_rules_check(monkeypatch):
    ran, said = bring_up(monkeypatch, "    inet 10.10.3.1/24 scope global lab3\n")
    assert ran == [("ifup", "lab3")] and "ifup lab3 reported a failure (error: lab3: cmd" in said[0]


def test_a_bridge_up_without_its_gateway_stops(monkeypatch):
    with pytest.raises(LabError, match="came up without its gateway 10.10.3.1"):
        bring_up(monkeypatch, "")


def test_settled_waits_for_the_reload_to_add_the_rules(monkeypatch):
    write_block()
    have = RULES[:2]
    fake_iptables(monkeypatch, have)
    monkeypatch.setattr(networks.time, "sleep", lambda s: have.extend(r for r in RULES[2:] if r not in have))
    networks.settled("3")
    assert have == RULES


def test_lines_that_are_no_simple_rule_are_not_checked(monkeypatch):
    from pvelab.ifaces import own_rules
    lines = ["iptables -A FORWARD -i lab3 -j DROP 2>/dev/null", "iptables -t nat -A POSTROUTING -j Y || true",
             "iptables -I FORWARD 1 -i lab3 -j ACCEPT", "iptables -A POSTROUTING -t nat -j MASQUERADE",
             'iptables -A FORWARD -m comment --comment "a > b" -j ACCEPT', "iptables -A FORWARD -i x -j ACCEPT|| true",
             "iptables -A FORWARD -o lab3 -j DROP"]
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n" + post_lines(lines, []) + "# lab.sh net 3 end\n")
    assert own_rules("3") == [("FORWARD -o lab3 -j DROP", "-A")]

def test_create_leaves_an_admins_plain_lines(monkeypatch):
    admin = "iface vmbr0 inet static\n        post-up   iptables -t nat -A POSTROUTING -j X\n"
    write_block()
    open(config.IFACES, "a").write(admin)
    fake_iptables(monkeypatch, list(RULES))
    networks.repair_rules("3")
    assert open(config.IFACES).read().endswith(admin)


def test_net_check_tries_the_network_when_iptables_cannot_tell(monkeypatch, capsys):
    from pvelab.commands import netcmd
    monkeypatch.setattr(netcmd, "check_bridge", lambda v: ("3", "lab3", ("10.10.3.250", 24, "10.10.3.1", ["10.10.3.1"])))
    monkeypatch.setattr(netcmd, "missing", lambda n: netcmd.die("iptables could not check X: lock"))
    monkeypatch.setattr(netcmd, "own_rules", lambda n: [("X", "-A")])
    monkeypatch.setattr(netcmd, "bridge_is_ours", lambda n: True)
    monkeypatch.setattr(netcmd.netcheck, "check", lambda br, mirror, static=None: [("address", True, "x"),
                                                                                   ("packages", True, "200")])
    netcmd.cmd_net_check(["check", "t97"])                       # the network works: no failure for it
    out = capsys.readouterr().out
    assert "? rules" in out and "could not check X" in out and "gets a network" in out


def test_a_plain_line_that_waits_becomes_add_once_and_a_quoted_one_stays_whole(monkeypatch):
    from pvelab import ifaces
    quoted = 'FORWARD -m comment --comment "a > b" -j ACCEPT'
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n"
                                   "        post-up   iptables -w 10 -A FORWARD -i lab3 -j DROP\n"
                                   f"        post-up   iptables -A {quoted}\n# lab.sh net 3 end\n")
    monkeypatch.setattr(ifaces, "run", lambda *a, **k: "")
    ifaces.make_rules_add_once()
    assert open(config.IFACES).read() == "# lab.sh net 3 begin\n" + post_lines(
        iptables(["FORWARD -i lab3 -j DROP", quoted])[0], []) + "# lab.sh net 3 end\n"

def test_settled_waits_on_while_iptables_cannot_tell(monkeypatch):
    write_block()
    answers = iter([networks.Unknown("lock"), networks.Unknown("lock")])

    def has_rule(r):
        if (a := next(answers, None)) is not None:
            raise a
        return True
    clock = iter(range(0, 100, 3))
    slept = []
    monkeypatch.setattr(networks, "has_rule", has_rule)
    monkeypatch.setattr(networks.time, "time", lambda: next(clock))
    monkeypatch.setattr(networks.time, "sleep", slept.append)
    networks.settled("3")                    # no stop, and no return at the first Unknown
    assert len(slept) == 2


def test_create_checks_a_new_nets_rules_again_after_its_isolation(monkeypatch):
    from pvelab.commands import create
    done = []
    monkeypatch.setattr(create, "is_external", lambda n: False)
    monkeypatch.setattr(create, "is_new_net", lambda n: True)
    monkeypatch.setattr(create, "ensure_bridge", lambda n: done.append("bridge"))
    monkeypatch.setattr(create, "set_isolation", lambda n, on: done.append("isolation"))
    monkeypatch.setattr(create, "repair_rules", lambda n: done.append("rules") or networks.die("t97: putting them back failed"))
    monkeypatch.setattr(create, "is_local", lambda h: True)
    monkeypatch.setattr(create.nets, "net_name", lambda n: "t97")
    monkeypatch.setattr(create.nets, "cidr", lambda n: "10.10.3.0/24")
    with pytest.raises(LabError, match="putting them back failed"):
        create.prepare_net("3", True, [])
    assert done == ["bridge", "isolation", "rules"]          # isolated, then no VMs without its rules


def test_a_rule_a_reload_added_after_the_drops_is_out_of_order_and_moved(monkeypatch):
    write_block()
    have = [RULES[0], RULES[1], RULES[3], RULES[2]]       # the ACCEPT came back after the DROP
    ran = fake_iptables(monkeypatch, have)
    monkeypatch.setattr(networks, "warn", lambda m: None)
    assert networks.missing("3") == (["out of order: FORWARD -i lab3 -o vmbr0 -j ACCEPT"], False)    # the one too far down
    networks.repair_rules("3")
    assert have == RULES
    assert ran[-4:] == ["*filter", "-A FORWARD -i lab3 -j DROP", "-D FORWARD -i lab3 -j DROP", "COMMIT"]


def test_canon_matches_what_iptables_prints():
    printed = "-A FORWARD -o lab3 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT".split()[1:]
    assert networks.canon(printed) == networks.canon("FORWARD -o lab3 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT".split())
    printed = "-A FORWARD -i lab3 -p tcp -m tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu".split()[1:]
    assert networks.canon(printed) == networks.canon("FORWARD -i lab3 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu".split())


def test_a_rule_not_found_as_printed_leaves_the_order_unknown(monkeypatch):
    write_block()
    fake_iptables(monkeypatch, list(RULES))
    monkeypatch.setattr(networks, "listed", lambda t, c: [])
    assert networks.missing("3") == ([], True)


def test_a_comment_line_is_not_checked():
    from pvelab.ifaces import own_rules
    open(config.IFACES, "w").write("# lab.sh net 3 begin\n        post-up   iptables -A FORWARD -i lab3 -j DROP # keep apart\n"
                                   "# lab.sh net 3 end\n")
    assert own_rules("3") == []


def test_no_such_chain_or_target_is_unknown_not_missing(monkeypatch):
    import subprocess
    monkeypatch.setattr(networks.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, "", "iptables v1.8.11 (legacy): Couldn't load target `TCPMSS':No such file or directory"))
    with pytest.raises(networks.Unknown):
        networks.has_rule("-t mangle FORWARD -i lab3 -j TCPMSS --clamp-mss-to-pmtu")
    monkeypatch.setattr(networks.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, "", "iptables: Bad rule (does a matching rule exist in that chain?).\n"))
    assert networks.has_rule("FORWARD -i lab3 -j DROP") is False


def test_net_check_says_when_the_order_was_not_checked(monkeypatch, capsys):
    from pvelab.commands import netcmd
    net_check(monkeypatch, [])
    monkeypatch.setattr(netcmd, "missing", lambda n: ([], True))
    netcmd.cmd_net_check(["check", "t97"])
    assert "order not checked" in capsys.readouterr().out


def test_forwarding_turned_on_also_when_iptables_cannot_tell(monkeypatch, forwarding_on):
    write_block()
    forwarding_on.write_text("0\n")
    fake_iptables(monkeypatch, [])
    monkeypatch.setattr(networks, "has_rule", lambda r: (_ for _ in ()).throw(networks.Unknown("lock")))
    monkeypatch.setattr(networks, "warn", lambda m: None)
    assert networks.repair_rules("3") == ["net.ipv4.ip_forward was off"] and forwarding_on.read_text() == "1\n"


def test_net_check_says_forwarding_is_off_whatever_the_rules(monkeypatch, capsys, forwarding_on):
    from pvelab.commands import netcmd
    forwarding_on.write_text("0\n")
    net_check_setup = monkeypatch.setattr
    net_check_setup(netcmd, "check_bridge", lambda v: ("3", "lab3", ("10.10.3.250", 24, "10.10.3.1", ["10.10.3.1"])))
    net_check_setup(netcmd, "own_rules", lambda n: [])
    net_check_setup(netcmd, "bridge_is_ours", lambda n: True)
    net_check_setup(netcmd.nets, "net_name", lambda n: "t97")
    net_check_setup(netcmd.netcheck, "check", lambda br, mirror, static=None: [("packages", False, "no answer")])
    with pytest.raises(LabError, match="net.ipv4.ip_forward is off - to put back in order: pvelab net fix t97"):
        netcmd.cmd_net_check(["check", "t97"])
    assert "✘ forward" in capsys.readouterr().out


def test_require_gateway_waits_for_this_hosts_reload(monkeypatch):
    answers = iter(["", "", "    inet 10.10.3.1/24 scope global lab3\n"] + ["    inet 10.10.3.1/24 scope global lab3\n"])
    slept = []
    monkeypatch.setattr(networks, "run", lambda *c, **k: next(answers))
    monkeypatch.setattr(networks.time, "sleep", slept.append)
    networks.require_gateway("lab3", "10.10.3.1", wait=60)
    assert len(slept) == 2


def test_require_gateway_with_a_prefix_waits_for_the_new_one(monkeypatch):
    answers = iter(["    inet 10.10.3.1/24 scope global lab3\n"] * 2 + ["    inet 10.10.3.1/23 scope global lab3\n"] * 2)
    slept = []
    monkeypatch.setattr(networks, "run", lambda *c, **k: next(answers))
    monkeypatch.setattr(networks.time, "sleep", slept.append)
    networks.require_gateway("lab3", "10.10.3.1", wait=60, prefix=23)
    assert len(slept) == 2
