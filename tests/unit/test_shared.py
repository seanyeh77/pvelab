"""A net's shared VM (pvelab create --shared): every student reaches it, students still not each other."""
import pytest

from pvelab import isolation
from pvelab.helptext import UsageError

from test_ux import fake_create

VMS = {201: "net30-student01-1", 202: "net30-student01-2", 203: "net30-student02-1", 210: "net30-shared"}


def test_shared_vm_reaches_and_is_reached_by_every_port():
    t = isolation.nft_table("30", VMS)
    assert '        iifname "tap210i0" accept\n' in t and '        oifname "tap210i0" accept\n' in t
    assert t.index('iifname "tap210i0"') < t.index("        drop\n")
    assert '"tap201i0" . "tap202i0"' in t and "tap203i0" not in t      # students still apart
    assert '"tap210i0" .' not in t and '. "tap210i0"' not in t          # not one student's VM
    assert "vxlan" not in t                                             # a vmbrN net: one host


def test_no_shared_vm_no_rules():
    t = isolation.nft_table("30", {201: "net30-student01", 202: "net30-student02"})
    assert 'ifname "tap' not in t and "accept\n        drop" in t       # only the same-student rule


def test_shared_vm_over_vxlan(monkeypatch):
    monkeypatch.setattr(isolation, "bridge", lambda n: f"lab{n}")
    monkeypatch.setattr(isolation, "is_sdn", lambda n: True)
    rules = ['oifname "vxlan_lab30" ether daddr 02:00:0a:0a:1e:02 accept',
             'oifname "vxlan_lab30" arp operation request arp daddr ip 10.10.30.20 accept',
             'iifname "vxlan_lab30" ether saddr 02:00:0a:0a:1e:02 accept']
    for gateway in (True, False):                # students on the gateway's host and on the others
        t = isolation.nft_table("30", VMS, gateway=gateway, ip="10.10.30.20")
        assert all(f"        {r}\n" in t for r in rules) and t.index(rules[-1]) < t.index("        drop\n")
    assert isolation.shared_mac(30) == "02:00:0a:0a:1e:02" != isolation.gw_mac(30)


def test_shared_ip_and_names():
    rows = {201: {"name": "net30-student01", "net": "30", "ip": "10.10.30.11"},
            210: {"name": "net30-shared", "net": "30", "ip": "10.10.30.12"},
            211: {"name": "net31-shared", "net": "31", "ip": "10.10.31.11"}}
    assert isolation.shared_ip("30", rows) == "10.10.30.12" and isolation.shared_ip("32", rows) is None
    assert isolation.is_shared("net30-shared", "30") and not isolation.is_shared("net30-shared", "31")
    assert isolation.students_only("30", VMS) == {k: v for k, v in VMS.items() if k != 210}


def test_create_shared_makes_one_vm_with_its_mac(monkeypatch, capsys):
    made, checked, macs = [], [], []
    create = fake_create(monkeypatch, made, checked)
    clone = create.clone_vm
    monkeypatch.setattr(create, "clone_vm", lambda *a, mac=None, **k: macs.append(mac) or clone(*a, **k))
    create.cmd_create(["--shared", "ubuntu26", "--net", "95", "--memory", "4G"])
    assert made == [("net95-shared", 9004, 2, 4096, None)] and macs == ["02:00:0a:0a:5f:02"]
    assert "create the shared VM net95-shared from" in capsys.readouterr().out


def test_create_shared_ignores_the_students_vms_per_student(monkeypatch):
    made = []
    create = fake_create(monkeypatch, made, [])
    rows = {201: {"name": "net95-student01-1", "net": "95", "password": "p"},
            202: {"name": "net95-student01-2", "net": "95", "password": "p"}}
    monkeypatch.setattr(create, "cred_rows", lambda: dict(rows))
    create.cmd_create(["--shared", "debian13", "--net", "95"])         # x2 net: a shared VM is still one VM
    assert [m[0] for m in made] == ["net95-shared"]
    rows[210] = {"name": "net95-shared", "net": "95", "password": "q"}
    assert create.per_student_on("95") == 2                   # and it does not count as a student
    made.clear()
    create.cmd_create(["2x2", "debian13", "--net", "95"])
    assert [m[0] for m in made] == ["net95-student01-1", "net95-student01-2", "net95-student02-1", "net95-student02-2"]


@pytest.mark.parametrize("args,why", [(["--shared"], "--shared needs --net"),
                                      (["--shared", "debian13", "x", "--net", "95"], "--shared makes one VM"),
                                      (["2", "debian13", "x", "--net", "95"], "too many arguments")])
def test_create_shared_usage(monkeypatch, args, why):
    create = fake_create(monkeypatch, [], [])
    with pytest.raises(UsageError, match=why):
        create.cmd_create(args)


def test_handout_leaves_the_shared_vm_out(monkeypatch, tmp_path, capsys):
    from pvelab.commands import handout
    rows = {201: {"name": "net30-student01", "net": "30", "user": "student", "password": "pw1"},
            210: {"name": "net30-shared", "net": "30", "user": "student", "password": "secret"}}
    monkeypatch.setattr(handout, "cred_rows", lambda: rows)
    monkeypatch.setattr(handout, "is_exposed", lambda n: True)
    monkeypatch.setattr(handout, "forwarded_on", lambda nets: set())
    monkeypatch.setattr(handout, "vm_names", lambda ids: {i: rows[i]["name"] for i in ids})
    out = tmp_path / "h.html"
    monkeypatch.setattr(handout, "targets", lambda a: [201, 210])
    handout.cmd_handout(["--net", "30", "-o", str(out)])
    assert "pw1" in out.read_text() and "secret" not in out.read_text()
    assert "pvelab handout net30-shared" in capsys.readouterr().out
    monkeypatch.setattr(handout, "targets", lambda a: [210])          # asked for alone: its card
    handout.cmd_handout(["net30-shared", "-o", str(out)])
    assert "secret" in out.read_text()


def test_require_quorum(monkeypatch):
    from pvelab import nodes
    from pvelab.util import LabError
    monkeypatch.setattr(nodes, "in_cluster", lambda: True)
    monkeypatch.setattr(nodes, "cfs_quorate", lambda: True)
    nodes.require_quorum("x")                                       # quorate: nothing
    monkeypatch.setattr(nodes, "cfs_quorate", lambda: False)
    monkeypatch.setattr(nodes, "members", lambda: {"workshop1": {"online": True}, "workshop2": {"online": False}})
    with pytest.raises(LabError) as e:
        nodes.require_quorum("no VM can be made")
    assert "workshop2 is offline" in str(e.value) and "no VM can be made" in str(e.value)
    assert str(e.value).endswith("gone for good:\n    pvelab node remove workshop2")
    monkeypatch.setattr(nodes, "in_cluster", lambda: False)
    nodes.require_quorum("x")                                       # no cluster: nothing


def test_create_stops_without_quorum_before_any_net(monkeypatch):
    from pvelab.util import LabError
    create = fake_create(monkeypatch, [], [])
    monkeypatch.setattr(create, "require_quorum", lambda doing: (_ for _ in ()).throw(LabError("no quorum")))
    monkeypatch.setattr(create, "pick_net", lambda *a: pytest.fail("a net was picked"))
    with pytest.raises(LabError, match="no quorum"):
        create.cmd_create(["2", "debian13", "--net", "97"])
