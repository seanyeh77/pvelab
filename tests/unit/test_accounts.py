"""create --accounts / --user / --password, the student a VM belongs to, and pvelab passwd."""
import pytest

from pvelab import accounts
from pvelab.util import LabError


def test_accounts_from_a_file_with_names_users_and_passwords(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("name,user,password\n,s1101001,\nalice01,s1101002,Abc-12345\n# a comment\n\n")
    got = accounts.parse(str(f), "prod", 1)
    assert [(a.name, a.user, a.password) for a in got] == [("prod-s1101001", "s1101001", None),
                                                           ("alice01", "s1101002", "Abc-12345")]


def test_accounts_without_a_header_and_inline():
    import os
    assert [(a.user, a.password) for a in accounts.parse("s1:Pass-1,s2,root:R00t-pass", "prod", 1)] == [
        ("s1", "Pass-1"), ("s2", None), ("root", "R00t-pass")]
    assert not os.path.exists("s1:Pass-1,s2")


@pytest.mark.parametrize("v, why", [
    ("Bad:x", "is not a login name"), ("1abc", "is not a login name"), ("a,a", "VM name\\(s\\) twice"),
    ("missing.csv", "no such file"), ("", "no accounts")])
def test_accounts_refused(v, why):
    with pytest.raises(LabError, match=why):
        accounts.parse(v, "prod", 1)


def test_long_names_with_several_vms_are_refused(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("name,user\n" + "x" * 62 + ",s1\n")
    accounts.parse(str(f), "prod", 1)
    with pytest.raises(LabError, match="is not a host name"):
        accounts.parse(str(f), "prod", 2)              # x..x-2: 64


def test_a_vms_student_comes_from_its_description(monkeypatch):
    from pvelab import isolation, inventory
    confs = {1: {"name": "alice01-1", "description": "managed-by=lab.sh net=5 student=alice01"},
             2: {"name": "alice01-2", "description": "managed-by=lab.sh net=5 student=alice01"},
             3: {"name": "net5-student02", "description": "managed-by=lab.sh net=5"}}
    monkeypatch.setattr(inventory, "read_conf", lambda i: confs.get(i))
    assert isolation.by_student({i: c["name"] for i, c in confs.items()}) == {"alice01": [1, 2],
                                                                                "net5-student02": [3]}


def fake(monkeypatch):
    """create with every host step faked: (made, the plan's lines)."""
    import importlib
    test_ux = importlib.import_module("test_ux")
    made, checked = [], []
    create = test_ux.fake_create(monkeypatch, made, checked)
    got = []

    def clone(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk, orig=None, board=None, mac=None, user="student",
              student=None):
        rows[vmid] = {"name": name, "user": user, "password": pw}
        got.append((name, user, pw, student))
    monkeypatch.setattr(create, "clone_vm", clone)
    monkeypatch.setattr("pvelab.ui.YES", True)       # the plan warns (short passwords, root): yes
    return create, got


def test_create_with_accounts_names_users_and_passwords(monkeypatch, tmp_path):
    create, got = fake(monkeypatch)
    f = tmp_path / "a.csv"
    f.write_text("name,user,password\n,s1101001,Pass-0001\nalice01,s1101002,\n")
    create.cmd_create(["debian13", "--net", "95", "--accounts", str(f)])
    assert [g[:2] for g in got] == [("net95-s1101001", "s1101001"), ("alice01", "s1101002")]
    assert got[0][2] == "Pass-0001" and len(got[1][2]) == 12
    assert [g[3] for g in got] == [None, None]          # the names tell: no student= in the description


def test_create_with_accounts_and_several_vms_each(monkeypatch):
    create, got = fake(monkeypatch)
    create.cmd_create(["2x2", "debian13", "--net", "95", "--accounts", "s1:Pass-0001,s2"])
    assert [g[0] for g in got] == ["net95-s1-1", "net95-s1-2", "net95-s2-1", "net95-s2-2"]
    assert got[0][2] == got[1][2] == "Pass-0001" and got[2][2] == got[3][2] != "Pass-0001"    # one per student
    assert got[0][3] is None                             # net95-s1-1: its name tells (net95-s1)


def test_create_accounts_count_and_conflicts(monkeypatch):
    from pvelab.helptext import UsageError
    create, _ = fake(monkeypatch)
    with pytest.raises(UsageError, match="has 2 student"):
        create.cmd_create(["3", "--net", "95", "--accounts", "s1,s2"])
    with pytest.raises(UsageError, match="not with --shared, --user or --password"):
        create.cmd_create(["--net", "95", "--accounts", "s1", "--user", "root"])


def test_create_user_per_vm_and_one_password(monkeypatch, tmp_path):
    from pvelab.helptext import UsageError
    create, got = fake(monkeypatch)
    create.cmd_create(["1x2", "debian13", "--net", "95", "--user", "root,student", "--password", "Root,pass-1"])
    assert [(g[1], g[2]) for g in got] == [("root", "Root,pass-1"), ("student", "Root,pass-1")]   # one per student
    with pytest.raises(UsageError, match="needs --net"):
        create.cmd_create(["debian13", "--accounts", "s1"])


def test_chosen_names_that_do_not_tell_their_student_carry_it(monkeypatch, tmp_path):
    create, got = fake(monkeypatch)
    f = tmp_path / "a.csv"
    f.write_text("name,user\nalice,s1\n")
    create.cmd_create(["1x2", "debian13", "--net", "95", "--accounts", str(f)])
    assert [(g[0], g[3]) for g in got] == [("alice-1", "alice"), ("alice-2", "alice")]


def test_cipassword_goes_to_the_snapshots_and_the_config_keeps_its_own(monkeypatch, tmp_path):
    from pvelab.commands import passwd
    conf = tmp_path / "201.conf"
    conf.write_text("cipassword: $5$old\nname: a\n[clean]\ncipassword: $5$old\n[special:cloudinit]\ncipassword: $5$old\n")

    def qm_set(*cmd, **k):                   # what qm set does: PVE stores it hashed in the config
        conf.write_text(conf.read_text().replace("cipassword: $5$old\nname", "cipassword: $5$new\nname", 1))
    monkeypatch.setattr(passwd, "conf_path", lambda i: str(conf))
    monkeypatch.setattr(passwd, "run", qm_set)
    monkeypatch.setattr(passwd, "on_node", lambda n, *c: list(c))
    passwd.set_cipassword(201, "New-pass", "workshop1", current=False)
    assert conf.read_text() == ("cipassword: $5$old\nname: a\n[clean]\ncipassword: $5$new\n"
                                "[special:cloudinit]\ncipassword: $5$old\n")
    conf.write_text("cipassword: $5$old\nname: a\n[clean]\ncipassword: $5$old\n")
    passwd.set_cipassword(201, "New-pass", "workshop1", current=True)
    assert conf.read_text() == "cipassword: $5$new\nname: a\n[clean]\ncipassword: $5$new\n"


def test_ssh_logs_in_as_the_vms_own_user(monkeypatch):
    from pvelab.commands import ssh
    monkeypatch.setattr(ssh, "resolve", lambda v: 218)
    monkeypatch.setattr(ssh, "cred_rows", lambda: {218: {"user": "root", "ip": "10.97.0.13"}})
    monkeypatch.setattr(ssh, "vm_ip", lambda i, rows: "10.97.0.13")
    ran = []
    monkeypatch.setattr(ssh.os, "execvp", lambda f, argv: ran.append(argv))
    ssh.cmd_ssh(["t97-admin"])
    assert "root@10.97.0.13" in ran[0]


def test_vendor_data_lets_root_with_a_password_log_in_over_ssh():
    from pvelab.templates import vendor_data
    v = vendor_data()
    assert "passwd -S root" in v and "PermitRootLogin yes" in v and "10-pvelab-root.conf" in v


def test_the_login_comes_from_pves_ciuser(monkeypatch):
    from pvelab import inventory
    assert inventory.login_of(1, {"user": "old"}, {"ciuser": "root"}) == "root"
    assert inventory.login_of(1, {"user": "s1"}, {}) == "s1" and inventory.login_of(1, None, {}) == "student"


def test_passwd_leaves_the_shared_vm_out_unless_named_alone(monkeypatch, capsys):
    from pvelab.commands import passwd
    rows = {201: {"name": "prod-student01", "net": "5", "user": "student", "password": "a", "ip": "-"},
            202: {"name": "prod-shared", "net": "5", "user": "student", "password": "b", "ip": "-"}}
    monkeypatch.setattr(passwd, "cred_rows", lambda: rows)
    monkeypatch.setattr(passwd, "targets", lambda a: [201, 202])
    monkeypatch.setattr(passwd, "is_shared", lambda name, n: name.endswith("-shared"))
    monkeypatch.setattr(passwd, "read_conf", lambda i: {"name": rows[i]["name"]})
    monkeypatch.setattr(passwd, "vm_statuses", lambda ids: {i: "stopped" for i in ids})
    monkeypatch.setattr(passwd, "node_of", lambda i: "workshop1")
    passwd.cmd_passwd(["--check", "--net", "prod"])
    out = capsys.readouterr().out
    assert "left out (a shared VM" in out and "prod-student01" in out and "prod-shared " not in out.split(")")[-1]


def test_the_shared_vms_name_is_no_students():
    with pytest.raises(LabError, match="shared VM's name"):
        accounts.parse("shared", "prod", 1)


def test_a_name_of_a_vm_pvelab_did_not_make_is_refused(monkeypatch):
    create, _ = fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {100: {"name": "web01", "description": ""}})
    monkeypatch.setattr(create.accounts, "parse", lambda v, n, per: [accounts.Account("web01", "s1", None)])
    with pytest.raises(LabError, match="web01 is taken by a VM pvelab did not make"):
        create.cmd_create(["debian13", "--net", "95", "--accounts", "s1"])


def test_quoted_csv_fields_from_a_spreadsheet(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text('name,user,password\nalice01,s1,"Ab,c9-xyz"\nbob01,s2,"Ab""c-xyz"\n')
    assert [a.password for a in accounts.parse(str(f), "prod", 1)] == ["Ab,c9-xyz", 'Ab"c-xyz']


def test_a_rerun_with_a_changed_list_says_where_it_differs(monkeypatch, capsys):
    create, _ = fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {201: {"name": "net95-s1", "tags": "lab-student",
                                                            "description": "managed-by=lab.sh net=95"}})
    monkeypatch.setattr(create, "cred_rows", lambda: {201: {"name": "net95-s1", "user": "s1", "password": "Old-pass-1",
                                                            "net": "95", "ip": "10.10.95.11"}})
    create.cmd_create(["debian13", "--net", "95", "--accounts", "s1:New-pass-1"])
    assert "the list differs from these VMs" in capsys.readouterr().err


def test_a_big5_file_with_a_home_path_and_spaces_after_commas(tmp_path, monkeypatch):
    f = tmp_path / "a.csv"
    f.write_bytes("# 學生名單\nname, user, password\n王小明01, s1, \"Pa,ss-word\"\n".replace("王小明01", "alice01")
                  .encode("cp950"))
    monkeypatch.setenv("HOME", str(tmp_path))
    got = accounts.parse("~/a.csv", "prod", 1)
    assert [(a.name, a.user, a.password) for a in got] == [("alice01", "s1", "Pa,ss-word")]
    f.write_bytes(b"user\n\xff\xfe\xfa\n")
    with pytest.raises(LabError, match="neither UTF-8 nor Big5"):
        accounts.parse("~/a.csv", "prod", 1)


def test_a_line_break_inside_a_cell_is_refused(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text('user,password\ns1,"Abc\n123"\n')
    with pytest.raises(LabError, match="tab or a line break"):
        accounts.parse(str(f), "prod", 1)


def test_empty_rows_spaces_inline_and_control_characters(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("user,password\r\ns1,Pass-0001\r\n,,\r\n,\r\n")
    assert [a.user for a in accounts.parse(str(f), "prod", 1)] == ["s1"]
    assert [(a.user, a.password) for a in accounts.parse("s1, s2:Pw-2", "prod", 1)] == [("s1", None), ("s2", "Pw-2")]
    assert not accounts.password_ok("a\rb") and not accounts.password_ok("a\tb") and accounts.password_ok("Ab c-1")


def test_empty_columns_on_the_right_are_ignored(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("name,user,password,\n,s1,Pass-0001,\n")
    assert [(a.user, a.password) for a in accounts.parse(str(f), "prod", 1)] == [("s1", "Pass-0001")]
    f.write_text("s1,Pass-0001,\n")
    assert [(a.user, a.password) for a in accounts.parse(str(f), "prod", 1)] == [("s1", "Pass-0001")]


def test_columns_with_no_header_are_left_out(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text(",user,,password\n,s1,,Pass-0001\n")
    assert [(a.user, a.password) for a in accounts.parse(str(f), "prod", 1)] == [("s1", "Pass-0001")]
    f.write_text("user,,password\ns1,note,Pass-0001\n")
    with pytest.raises(LabError, match="a value in column 2, which has no header"):
        accounts.parse(str(f), "prod", 1)
