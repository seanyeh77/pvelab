"""create --accounts / --user / --password, the student a VM belongs to, and pvelab passwd."""
import pytest

from pvelab import accounts
from pvelab.util import LabError


def test_accounts_from_a_file_with_names_users_and_passwords(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("# a comment\nname,user,password\n,s1101001,\nalice01,s1101002,Abc-12345\n\n")
    got = accounts.parse(str(f), "prod", 1)
    assert [(a.name, a.user, a.password) for a in got] == [("prod-s1101001", "s1101001", None),
                                                           ("alice01", "s1101002", "Abc-12345")]


def test_accounts_without_a_header_and_inline():
    import os
    assert [(a.user, a.password) for a in accounts.parse("s1:Pass-1,s2,root:R00t-pass", "prod", 1)] == [
        ("s1", "Pass-1"), ("s2", None), ("root", "R00t-pass")]
    assert not os.path.exists("s1:Pass-1,s2")


@pytest.mark.parametrize("v, why", [
    ("Bad:x", "is not a login name"), ("1abc", "is not a login name"), ("a,a", "owner\\(s\\) twice"),
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
    from pvelab import owners
    confs = {1: {"name": "alice01-1", "description": "managed-by=lab.sh net=5 student=alice01"},
             2: {"name": "alice01-2", "description": "managed-by=lab.sh net=5 student=alice01"},
             3: {"name": "net5-student02", "description": "managed-by=lab.sh net=5"}}
    monkeypatch.setattr(owners, "read_conf", lambda i: confs.get(i))
    monkeypatch.setattr(owners.nets, "net_name", lambda n: f"net{n}")
    assert owners.by_owner({i: c["name"] for i, c in confs.items()}) == {"alice01": [1, 2], "student02": [3]}


def fake(monkeypatch):
    """create with every host step faked: (made, the plan's lines)."""
    import importlib
    test_ux = importlib.import_module("test_ux")
    made, checked = [], []
    create = test_ux.fake_create(monkeypatch, made, checked)
    got = []

    def clone(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk, orig=None, board=None, mac=None, user="student",
              owner=None, shared=False, prefix=None):
        rows[vmid] = {"name": name, "user": user, "password": pw}
        got.append((name, user, pw, owner))
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
    assert [g[3] for g in got] == ["s1101001", "alice01"]     # owner= in every description


def test_create_with_accounts_and_several_vms_each(monkeypatch):
    create, got = fake(monkeypatch)
    create.cmd_create(["2x2", "debian13", "--net", "95", "--accounts", "s1:Pass-0001,s2"])
    assert [g[0] for g in got] == ["net95-s1-1", "net95-s1-2", "net95-s2-1", "net95-s2-2"]
    assert got[0][2] == got[1][2] == "Pass-0001" and got[2][2] == got[3][2] != "Pass-0001"    # one per student
    assert got[0][3] == got[1][3] == "s1"                # net95-s1-1, -2: owner s1


def test_create_accounts_count_and_conflicts(monkeypatch):
    from pvelab.helptext import UsageError
    create, _ = fake(monkeypatch)
    with pytest.raises(UsageError, match="has 2 account"):
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
    assert inventory.login_of(1, {"user": "old"}, {"ciuser": "root", "ide2": "local-lvm:vm-1-cloudinit"}) == "root"
    assert inventory.login_of(1, {"user": "s1"}, {}) == "s1" and inventory.login_of(1, None, {}) == "student"


def test_passwd_leaves_the_shared_vm_out_unless_named_alone(monkeypatch, capsys):
    from pvelab.commands import passwd
    rows = {201: {"name": "prod-student01", "net": "5", "user": "student", "password": "a", "ip": "-"},
            202: {"name": "prod-shared", "net": "5", "user": "student", "password": "b", "ip": "-"}}
    monkeypatch.setattr(passwd, "cred_rows", lambda: rows)
    monkeypatch.setattr(passwd, "targets", lambda a: [201, 202])
    monkeypatch.setattr("pvelab.owners.read_conf", lambda i: {"name": rows[i]["name"], "description":
                        "managed-by=lab.sh net=5" + (" role=shared" if i == 202 else " owner=student01")})
    monkeypatch.setattr(passwd, "vm_statuses", lambda ids: {i: "stopped" for i in ids})
    monkeypatch.setattr(passwd, "node_of", lambda i: "workshop1")
    passwd.cmd_passwd(["--check", "--net", "prod"])
    out = capsys.readouterr().out
    assert "left out (a shared VM" in out and "prod-student01" in out and "prod-shared " not in out.split(")")[-1]


def test_the_shared_vms_name_is_no_students():
    with pytest.raises(LabError, match="'shared' marks"):
        accounts.parse("shared", "prod", 1)


def test_a_name_of_a_vm_pvelab_did_not_make_is_refused(monkeypatch):
    create, _ = fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {100: {"name": "web01", "description": ""}})
    monkeypatch.setattr(create.accounts, "parse", lambda v, n, per, names=(): [accounts.Account("web01", "s1", None, "web01")])
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


def test_a_cell_starting_with_hash_and_a_column_twice(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("# roster\npassword,user\n#Secret-123,s1\nXyz-1234567,s2\n")
    assert [(a.user, a.password) for a in accounts.parse(str(f), "prod", 1)] == [("s1", "#Secret-123"),
                                                                                 ("s2", "Xyz-1234567")]
    f.write_text("user,password,user\ns1,p,s2\n")
    with pytest.raises(LabError, match="column\\(s\\) twice: user"):
        accounts.parse(str(f), "prod", 1)


def test_a_quoted_comment_at_the_top(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text('"# roster, class A",,\nname,user,password\n,s1,\n')
    assert [a.user for a in accounts.parse(str(f), "prod", 1)] == ["s1"]


def test_an_inline_password_may_hold_a_slash():
    assert [a.password for a in accounts.parse("s1:Abc/12345", "prod", 1)] == ["Abc/12345"]
    with pytest.raises(LabError, match="no such file"):
        accounts.parse("~/nope/roster.csv", "prod", 1)


def test_rename_refuses_a_used_name_and_bad_names(monkeypatch):
    from pvelab import readdress
    rows = {201: {"name": "prod-s1-1", "net": "5", "user": "s1", "password": "p", "ip": "10.5.0.11"}}
    monkeypatch.setattr(readdress, "resolve", lambda v: 201)
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "inventory", lambda: {202: {"name": "taken"}})
    for new, why in (("Bad_name", "not a host name"), ("all", "means something else"), ("net5", "means something"),
                     ("taken", "in use already"), ("prod-s1-1", "has that name already")):
        with pytest.raises(LabError, match=why):
            readdress.vm_rename("prod-s1-1", new)


def test_wizard_rename(monkeypatch):
    import importlib
    test_ux = importlib.import_module("test_ux")
    from pvelab import wizard
    test_ux.answers(monkeypatch, "net30-student01", "Bad!", "all", "net30-alice")
    assert wizard.Wizard(test_ux.DATA).rename() == ["rename", "net30-student01", "net30-alice"]


def test_create_makes_no_new_vm_in_place_of_a_renamed_one(monkeypatch, capsys):
    create, got = fake(monkeypatch)
    managed = {"tags": "lab-student"}
    monkeypatch.setattr(create, "inventory", lambda: {
        201: {**managed, "name": "net95-student01", "description": "managed-by=lab.sh net=95"},
        202: {**managed, "name": "net95-bob", "description": "managed-by=lab.sh net=95 student=net95-student02"}})
    create.cmd_create(["3", "debian13", "--net", "95"])
    assert [g[0] for g in got] == ["net95-student03"]          # student02 is bob now


def test_a_failed_rename_leaves_the_credentials(monkeypatch):
    from pvelab import readdress
    rows = {201: {"name": "prod-s1", "net": "5", "user": "s1", "password": "p", "ip": "10.5.0.11"}}
    saved = []
    monkeypatch.setattr(readdress, "resolve", lambda v: 201)
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "cred_save", lambda r: saved.append(r))
    monkeypatch.setattr(readdress, "inventory", lambda: {})
    monkeypatch.setattr(readdress, "read_conf", lambda i: {"name": "prod-s1"})
    monkeypatch.setattr(readdress, "run", lambda *a, **k: "")
    monkeypatch.setattr(readdress, "on_node", lambda n, *c: list(c))

    class FakeChange:
        def __init__(self, n, what, vms=None):
            self.plan, self.vms, self.node = readdress.Plan(), set(vms), {201: "workshop1"}

        def warnings(self, vms=None):
            pass

        def start(self, dry):
            return True

        def set_vms(self, key, values):
            self.vms = set()                 # the config could not change (locked)
    monkeypatch.setattr(readdress, "Change", FakeChange)
    with pytest.raises(LabError, match="may be renamed in part"):
        readdress.vm_rename("prod-s1", "prod-alice")
    assert saved == []


def test_change_of_one_vm_warns_about_it_only(monkeypatch):
    from pvelab import readdress
    rows = {201: {"name": "a", "net": "5"}, 202: {"name": "b", "net": "5"}}
    monkeypatch.setattr(readdress, "require_quorum", lambda doing: None)
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "node_of", lambda i: "workshop1")
    monkeypatch.setattr(readdress, "vm_statuses", lambda ids: {i: "running" for i in ids})
    monkeypatch.setattr(readdress, "in_sdn", lambda n: True)
    c = readdress.Change("5", "name", vms={201})
    c.warnings()
    assert set(c.rows) == {201} and c.vms == {201} and not c.sdn_too
    assert not any("SDN" in w for w, *_ in c.plan.warnings)


def test_create_fills_only_the_missing_vms_of_a_student_with_a_renamed_one(monkeypatch):
    create, got = fake(monkeypatch)
    managed = {"tags": "lab-student"}
    monkeypatch.setattr(create, "inventory", lambda: {
        202: {**managed, "name": "net95-db", "description": "managed-by=lab.sh net=95 student=net95-student01"}})
    monkeypatch.setattr(create, "per_owner_on", lambda n, confs=None: 2)
    create.cmd_create(["1x2", "debian13", "--net", "95"])
    assert [g[0] for g in got] == ["net95-student01-1"]       # one missing, not two


def test_a_rename_that_stopped_half_way_can_be_run_again(monkeypatch):
    from pvelab import readdress
    rows = {201: {"name": "prod-s1", "net": "5", "user": "s1", "password": "p", "ip": "10.5.0.11"}}
    monkeypatch.setattr(readdress, "resolve", lambda v: readdress.die("no student VM named " + v))
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "inventory", lambda: {201: {"name": "prod-alice"}})     # PVE has it already
    monkeypatch.setattr(readdress, "is_managed", lambda c: bool(c))
    monkeypatch.setattr(readdress, "read_conf", lambda i: {"name": "prod-alice"})
    seen = []
    monkeypatch.setattr(readdress, "Change", lambda n, what, vms=None: seen.append(vms) or (_ for _ in ()).throw(
        LabError("stop here")))
    with pytest.raises(LabError, match="stop here"):
        readdress.vm_rename("prod-s1", "prod-alice")          # found by its recorded name; its own name is no clash
    assert seen == [{201}]


def test_rename_completion():
    from pvelab.commands import complete
    data = {"vms": ["net30-student01"]}.get
    assert complete.candidates(["rename", ""], 2, data) == ["net30-student01"]
    assert complete.candidates(["rename", "net30-student01", "--d"], 3, data) == ["--dry-run"]


def test_a_deleted_vms_leftover_row_is_no_rename_target(monkeypatch):
    from pvelab import readdress
    rows = {201: {"name": "prod-s1", "net": "5", "user": "s1", "password": "p", "ip": "10.5.0.11"}}
    monkeypatch.setattr(readdress, "resolve", lambda v: readdress.die("no student VM named " + v))
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "inventory", lambda: {})                 # deleted in PVE
    with pytest.raises(LabError, match="no student VM named prod-s1"):
        readdress.vm_rename("prod-s1", "prod-alice")


def test_create_stops_when_a_slots_name_is_another_students_renamed_vm(monkeypatch):
    create, got = fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {
        205: {"tags": "lab-student", "name": "net95-student01",
              "description": "managed-by=lab.sh net=95 student=net95-student05"}})
    with pytest.raises(LabError, match="net95-student01 is owner student05's VM"):
        create.cmd_create(["5", "debian13", "--net", "95"])


def test_create_with_a_name_or_a_prefix(monkeypatch):
    create, got = fake(monkeypatch)
    create.cmd_create(["1", "debian13", "--net", "95", "--name", "web01", "--user", "root"])
    assert [(g[0], g[1], g[3]) for g in got] == [("web01", "root", "web01")]
    got.clear()
    create.cmd_create(["1x2", "debian13", "--net", "95", "--name", "net95-db"])
    assert [(g[0], g[3]) for g in got] == [("net95-db-1", "db"), ("net95-db-2", "db")]   # the owner without the net
    got.clear()
    create.cmd_create(["2", "debian13", "--net", "95", "--prefix", "team"])
    assert [(g[0], g[3]) for g in got] == [("net95-team01", "team01"), ("net95-team02", "team02")]


@pytest.mark.parametrize("args, why", [
    (["2", "--net", "95", "--name", "web01"], "one owner"),
    (["1", "--net", "95", "--name", "Web_01"], "not a host name"),
    (["1", "--net", "95", "--name", "all"], "means something else"),
    (["2", "--net", "95", "--prefix", "web01"], "lowercase letters only"),
    (["2", "--net", "95", "--prefix", "shared"], "lowercase letters only"),
    (["1", "--net", "95", "--name", "a", "--prefix", "b"], "one of them"),
    (["--net", "95", "--accounts", "s1", "--name", "a"], "not with --accounts"),
    (["--shared", "--net", "95", "--prefix", "a"], "the shared VM takes --name"),
])
def test_create_name_and_prefix_refused(monkeypatch, args, why):
    from pvelab.helptext import UsageError
    create, _ = fake(monkeypatch)
    with pytest.raises(UsageError, match=why):
        create.cmd_create(args)
