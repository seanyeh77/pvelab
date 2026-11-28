"""net pool / net token: the pveum commands and their order, what --off undoes, the role and ACLs, the secret
kept out of the output and the log, and clone_vm's --pool."""
import ipaddress
import json
import os
import stat
import sys

import pytest

from pvelab import config, nets, pools, ui, util
from pvelab.commands import netcmd
from pvelab.util import LabError
from test_profiles import clone_commands, TODAY

SECRET = "SECRET-MARKER-5f1c"
POOL = "pool:pvelab-demo:pvelab net demo:201,202::\n"
TOKEN = ("role:LabAudit:VM.Audit:\nuser:pvelab-demo@pve:1:0::::pvelab net demo API only::\n"
         "token:pvelab-demo@pve!api:0:1::\n"
         "acl:1:/pool/pvelab-demo:pvelab-demo@pve:LabAudit:\n"
         "acl:1:/pool/pvelab-demo:pvelab-demo@pve!api:LabAudit:\n")


@pytest.fixture
def demo(monkeypatch):
    """Net 40 called demo with VMs 201 (alice) and 202 (its shared VM); pveum faked: [argv] it was given."""
    nets.register("40", "demo", ipaddress.ip_network("10.10.40.0/24"))
    inv = {201: {"name": "demo-alice", "_node": "pve1"}, 202: {"name": "demo-shared", "_node": "pve1"},
           203: {"name": "other", "_node": "pve1"}}
    monkeypatch.setattr(netcmd, "inventory", lambda: inv)
    monkeypatch.setattr(netcmd, "on_net", lambda n, i: {v: i[v] for v in (201, 202)} if n == "40" else {})
    ran = []

    def fake(*argv, **kw):
        ran.append(list(argv))
        return json.dumps({"full-tokenid": "pvelab-demo@pve!api", "info": {"privsep": "1"}, "value": SECRET}) \
            if argv[1:4] == ("user", "token", "add") else ""
    monkeypatch.setattr(pools, "run", fake)
    return ran


def user_cfg(text):
    with open(config.USER_CFG, "w") as f:
        f.write("user:root@pam:1:0:::::\n" + text)


def pveum(ran):
    assert all(a[0] == "pveum" for a in ran)
    return [a[1:] for a in ran]


def test_pool_takes_the_nets_vms_shared_one_too(demo):
    netcmd.cmd_net_pool(["pool", "demo"])
    assert pveum(demo) == [["pool", "add", "pvelab-demo", "--comment", "pvelab net demo"],
                           ["pool", "modify", "pvelab-demo", "--vms", "201,202"]]


def test_pool_again_adds_only_what_is_missing(demo, capsys):
    user_cfg("pool:pvelab-demo::201::\n")
    netcmd.cmd_net_pool(["pool", "demo"])
    assert pveum(demo) == [["pool", "modify", "pvelab-demo", "--vms", "202"]]
    user_cfg(POOL)
    netcmd.cmd_net_pool(["pool", "demo"])
    assert len(demo) == 1 and "has its pool pvelab-demo already" in capsys.readouterr().out


def test_a_vm_in_another_pool_is_left_there(demo, monkeypatch, capsys):
    monkeypatch.setattr(ui, "YES", True)
    user_cfg("pool:admins::202::\n")
    netcmd.cmd_net_pool(["pool", "demo"])
    assert pveum(demo)[-1] == ["pool", "modify", "pvelab-demo", "--vms", "201"]
    assert "demo-shared is in pool admins already: left there" in capsys.readouterr().out


def test_pool_off_empties_then_removes_it(demo):
    user_cfg(POOL)
    netcmd.cmd_net_pool(["pool", "demo", "--off"])
    assert pveum(demo) == [["pool", "modify", "pvelab-demo", "--vms", "201,202", "--delete", "1"],
                           ["pool", "delete", "pvelab-demo"]]


def test_pool_off_refused_while_the_token_is_there(demo):
    user_cfg(POOL + TOKEN)
    with pytest.raises(LabError, match="net token demo --off first"):
        netcmd.cmd_net_pool(["pool", "demo", "--off"])
    assert demo == []


def test_dry_run_changes_nothing(demo, capsys):
    netcmd.cmd_net_pool(["pool", "demo"], dry=True)
    user_cfg(POOL)
    netcmd.cmd_net_token(["token", "demo"], dry=True)
    assert demo == [] and "make token pvelab-demo@pve!api" in capsys.readouterr().out
    assert not os.path.exists(pools.token_file("40"))


def test_token_needs_the_pool(demo):
    with pytest.raises(LabError, match="pvelab net pool demo first"):
        netcmd.cmd_net_token(["token", "demo"])


def test_token_commands_and_their_order(demo):
    user_cfg(POOL)
    netcmd.cmd_net_token(["token", "demo"])
    assert pveum(demo) == [
        ["role", "add", "LabAudit", "--privs", "VM.Audit"],
        ["user", "add", "pvelab-demo@pve", "--comment", "pvelab net demo: API only"],
        ["acl", "modify", "/pool/pvelab-demo", "--users", "pvelab-demo@pve", "--roles", "LabAudit"],
        ["user", "token", "add", "pvelab-demo@pve", "api", "--privsep", "1", "--output-format", "json"],
        ["acl", "modify", "/pool/pvelab-demo", "--tokens", "pvelab-demo@pve!api", "--roles", "LabAudit"]]


def test_two_acls_on_the_pool_only_with_one_role(demo):
    user_cfg(POOL)
    netcmd.cmd_net_token(["token", "demo"])
    acls = [a for a in pveum(demo) if a[0] == "acl"]
    assert {a[2] for a in acls} == {"/pool/pvelab-demo"} and {a[-1] for a in acls} == {"LabAudit"}
    assert sorted(a[3:5] for a in acls) == [["--tokens", "pvelab-demo@pve!api"], ["--users", "pvelab-demo@pve"]]
    roles = [a for a in pveum(demo) if a[0] == "role"]
    assert roles == [["role", "add", "LabAudit", "--privs", "VM.Audit"]]       # exactly VM.Audit


def test_an_existing_role_with_these_privs_is_used(demo):
    user_cfg(POOL + "role:LabAudit:VM.Audit:\n")
    netcmd.cmd_net_token(["token", "demo"])
    assert not any(a[0] == "role" for a in pveum(demo))


def test_an_existing_role_with_other_privs_stops_it(demo):
    user_cfg(POOL + "role:LabAudit:VM.Audit,VM.PowerMgmt:\n")
    with pytest.raises(LabError, match="LabAudit exists with other privileges"):
        netcmd.cmd_net_token(["token", "demo"])
    assert demo == []


def test_the_secret_only_in_its_file(demo, monkeypatch, capsys, tmp_path):
    """pveum's answer through the real run() (a stand-in program prints it): not in stdout, stderr or the
    log, nor an argument of any command; the file is root's, mode 0600."""
    args = []

    def stand_in(*argv, **kw):
        args.append(argv)
        out = json.dumps({"value": SECRET}) if argv[1:4] == ("user", "token", "add") else ""
        return util.run(sys.executable, "-c", f"import sys; print({out!r}); print('noise', file=sys.stderr)", **kw)
    monkeypatch.setattr(pools, "run", stand_in)
    log = ui.start_log(str(tmp_path), "net-token")
    user_cfg(POOL)
    netcmd.cmd_net_token(["token", "demo"])
    out = capsys.readouterr()
    path = pools.token_file("40")
    assert SECRET not in out.out + out.err and SECRET not in open(log).read() and "noise" in open(log).read()
    assert not any(SECRET in str(a) for argv in args for a in argv)
    assert path in out.out and "pvelab-demo@pve!api" in out.out
    assert open(path).read() == f"pvelab-demo@pve!api={SECRET}\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and path.endswith("/tokens/demo.token")


def test_a_second_token_is_refused_and_names_the_file(demo):
    user_cfg(POOL + TOKEN)
    with pytest.raises(LabError, match=r"has an API token already .*tokens/demo.token.*--rotate"):
        netcmd.cmd_net_token(["token", "demo"])
    assert demo == []


def test_rotate_makes_the_token_again_with_its_acl(demo):
    user_cfg(POOL + TOKEN)
    netcmd.cmd_net_token(["token", "demo", "--rotate"])
    assert pveum(demo) == [
        ["user", "token", "remove", "pvelab-demo@pve", "api"],
        ["user", "token", "add", "pvelab-demo@pve", "api", "--privsep", "1", "--output-format", "json"],
        ["acl", "modify", "/pool/pvelab-demo", "--tokens", "pvelab-demo@pve!api", "--roles", "LabAudit"]]
    assert SECRET in open(pools.token_file("40")).read()


def test_rotate_puts_back_a_users_acl_removed_by_hand(demo):
    user_cfg(POOL + TOKEN.replace("acl:1:/pool/pvelab-demo:pvelab-demo@pve:LabAudit:\n", ""))
    netcmd.cmd_net_token(["token", "demo", "--rotate"])
    assert ["acl", "modify", "/pool/pvelab-demo", "--users", "pvelab-demo@pve", "--roles", "LabAudit"] in pveum(demo)


def test_rotate_without_a_token(demo):
    user_cfg(POOL)
    with pytest.raises(LabError, match="has no API token yet"):
        netcmd.cmd_net_token(["token", "demo", "--rotate"])


def test_token_off_undoes_each_step(demo):
    user_cfg(POOL + TOKEN)
    os.makedirs(pools.TOKEN_DIR)
    open(pools.token_file("40"), "w").close()
    netcmd.cmd_net_token(["token", "demo", "--off"])
    assert pveum(demo) == [
        ["acl", "delete", "/pool/pvelab-demo", "--users", "pvelab-demo@pve", "--roles", "LabAudit"],
        ["acl", "delete", "/pool/pvelab-demo", "--tokens", "pvelab-demo@pve!api", "--roles", "LabAudit"],
        ["user", "token", "remove", "pvelab-demo@pve", "api"],
        ["user", "delete", "pvelab-demo@pve"],
        ["role", "delete", "LabAudit"]]
    assert not os.path.exists(pools.token_file("40"))


def test_token_off_keeps_the_role_another_net_uses(demo):
    user_cfg(POOL + TOKEN + "acl:1:/pool/pvelab-prod:pvelab-prod@pve!api:LabAudit:\n")
    netcmd.cmd_net_token(["token", "demo", "--off"])
    assert ["role", "delete", "LabAudit"] not in pveum(demo)


def test_token_off_without_one(demo):
    user_cfg(POOL)
    with pytest.raises(LabError, match="has no API token"):
        netcmd.cmd_net_token(["token", "demo", "--off"])


@pytest.mark.parametrize("argv", [["pool"], ["pool", "demo", "--rotate"], ["token", "demo", "--rotate", "--off"],
                                  ["token", "a", "b"]])
def test_usage(demo, argv):
    from pvelab.helptext import UsageError
    with pytest.raises(UsageError):
        (netcmd.cmd_net_pool if argv[0] == "pool" else netcmd.cmd_net_token)(argv)


def test_no_pool_for_a_plain_bridge(demo, monkeypatch):
    monkeypatch.setattr(netcmd, "net_arg", lambda v: v)
    with pytest.raises(LabError, match="not pvelab's network"):
        netcmd.cmd_net_pool(["pool", "dhcp"])


def test_clone_with_the_nets_pool(monkeypatch):
    user_cfg("pool:pvelab-net95::::\n")
    seen = [s for s in clone_commands(monkeypatch, user="user") if s[0] != "record"]
    assert seen == [TODAY[0] + ["--pool", "pvelab-net95"]] + TODAY[1:]


def test_clone_without_a_pool_runs_todays_commands(monkeypatch):
    user_cfg("pool:pvelab-net96::::\n")                 # another net's
    assert [s for s in clone_commands(monkeypatch, user="user") if s[0] != "record"] == TODAY


def test_rename_refused_while_the_net_has_a_pool(demo):
    user_cfg(POOL + TOKEN)
    with pytest.raises(LabError, match="net token demo --off, then pvelab net pool demo --off first"):
        netcmd.cmd_net_rename(["rename", "demo", "prod"])
    user_cfg(POOL)
    with pytest.raises(LabError, match="has a PVE pool named after it: pvelab net pool demo --off first"):
        netcmd.cmd_net_rename(["rename", "demo", "prod"])


def test_net_remove_takes_the_token_and_pool(demo, monkeypatch):
    user_cfg("pool:pvelab-demo::::\n" + TOKEN)
    monkeypatch.setattr(netcmd, "local_net", lambda v, verb: "40")
    monkeypatch.setattr(netcmd, "guests_on", lambda br: [])
    monkeypatch.setattr(netcmd, "cred_rows", lambda: {})
    monkeypatch.setattr(netcmd, "taken_over", lambda n: False)
    monkeypatch.setattr(netcmd, "in_sdn", lambda n: False)
    monkeypatch.setattr(netcmd, "remove_bridge", lambda n, quiet: demo.append(["remove_bridge", n]))
    netcmd.cmd_net_remove(["remove", "demo"])
    assert [a[1:3] for a in demo] == [["acl", "delete"], ["acl", "delete"], ["user", "token"], ["user", "delete"],
                                      ["role", "delete"], ["pool", "delete"], ["40"]]


def test_user_cfg_parsing():
    text = POOL + TOKEN + "pool:empty::::\n"
    assert pools.pools(text) == {"pvelab-demo": {"vms": [201, 202], "storage": []}, "empty": {"vms": [], "storage": []}}
    assert pools.roles(text) == {"LabAudit": {"VM.Audit"}}
    assert pools.tokens(text) == {"pvelab-demo@pve!api"} and "pvelab-demo@pve" in pools.users(text)
    assert ("/pool/pvelab-demo", "pvelab-demo@pve!api", "LabAudit") in pools.acls(text)


def test_destroying_a_nets_last_vm_names_its_pool_and_token(demo, monkeypatch):
    from pvelab.commands import destroy
    monkeypatch.setattr(destroy, "taken_over", lambda n: False)
    monkeypatch.setattr(destroy, "bridge", lambda n: "lab40")
    user_cfg("pool:pvelab-demo::::\n" + TOKEN)
    assert destroy.what_goes("40") == "bridge lab40, its API token pvelab-demo@pve!api with its user, its PVE pool pvelab-demo"
    pools.remove_with_net("40")
    assert [a[1:3] for a in demo][-2:] == [["role", "delete"], ["pool", "delete"]]
