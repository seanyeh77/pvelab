"""create --profile: profile files, the order clone_vm sets them in, and nothing new without one."""
import pytest

from pvelab import lifecycle, profiles, templates
from pvelab.util import LabError
from test_ux import fake_create

BENCH = """
[qm]
cpu = "host"
balloon = 0
affinity = "0-3"

[[disk]]
size = "50G"

[[disk]]
size = 2

[cloud-init]
runcmd = ["sed -i '/update_etc_hosts/d' /etc/cloud/cloud.cfg", ["touch", "/x"]]
packages = ["fio"]
"""


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(profiles, "_loaded", {})
    (tmp_path / "bench.toml").write_text(BENCH)
    (tmp_path / "plain.toml").write_text('[qm]\nnuma = true\n')
    return tmp_path


def test_parse_reads_every_part(profile_dir):
    p = profiles.load("bench")
    assert p.qm == (("cpu", "host"), ("balloon", "0"), ("affinity", "0-3"))
    assert p.disks == (50, 2) and p.disk_gb == 52
    assert p.qm_args("local-lvm") == ["--cpu", "host", "--balloon", "0", "--affinity", "0-3",
                                      "--scsi2", "local-lvm:50", "--scsi3", "local-lvm:2"]
    assert p.snippet == "pvelab-bench.yaml"
    assert profiles.load("plain").qm == (("numa", "1"),) and profiles.load("plain").snippet is None
    assert profiles.names() == ["bench", "plain"]


@pytest.mark.parametrize("text,why", [
    ("[qm]\nmemory = 4096\n", "pvelab sets it itself"),
    ("[qm]\nnet0 = \"virtio\"\n", "pvelab sets it itself"),
    ("[qm]\nipconfig0 = \"ip=dhcp\"\n", "pvelab sets it itself"),
    ("[qm]\nhookscript = \"local:snippets/x\"\n", "not allowed"),
    ("[qm]\nscsi1 = \"x\"\n", "not allowed"),
    ("[hook]\nx = 1\n", "unknown section"),
    ("[qm]\naffinity = \"0-3; reboot\"\n", "not a value"),
    ("[qm]\nballoon = -1\n", "not a value"),
    ("[[disk]]\nsize = \"50X\"\n", "GB like"),
    ("[[disk]]\nsize = \"50G\"\nstorage = \"nfs\"\n", "only size"),
    ("[cloud-init]\nusers = [\"x\"]\n", "not allowed"),
    ("[cloud-init]\nruncmd = \"x\"\n", "a list"),
    ("[qm\n", "not TOML"),
])
def test_parse_refuses(text, why):
    with pytest.raises(LabError, match=why):
        profiles.parse("x", text)


def test_missing_file_and_bad_name_refused(profile_dir):
    with pytest.raises(LabError, match="no .*nope.toml.*there are: bench, plain"):
        profiles.load("nope")
    with pytest.raises(LabError, match="profile name"):
        profiles.load("../etc/passwd")


def test_one_profile_per_vm(profile_dir):
    from pvelab.commands.create import per_vm
    got = per_vm("--profile", "bench,plain,plain", 3, profiles.per_vm_arg)
    assert [p.name for p in got] == ["bench", "plain", "plain"]
    assert [p and p.name for p in per_vm("--profile", "bench,", 2, profiles.per_vm_arg)] == ["bench", None]
    assert [p.name for p in per_vm("--profile", "bench", 2, profiles.per_vm_arg)] == ["bench", "bench"]
    with pytest.raises(LabError, match="several values only with OWNERSxVMS"):
        per_vm("--profile", "bench,plain", 1, profiles.per_vm_arg)


def test_snippet_is_pvelabs_vendor_data_plus_the_profiles(profile_dir):
    text = profiles.snippet_text(profiles.load("bench"))
    base = templates.vendor_data()
    assert templates.vendor_data({}) == base         # without a profile: the same text as always
    assert text.startswith(base.split("packages:")[0])
    assert 'packages: [qemu-guest-agent, "fio"]\n' in text
    assert text.endswith(base.split("runcmd:\n")[1] + "  - \"sed -i '/update_etc_hosts/d' /etc/cloud/cloud.cfg\"\n"
                         "  - [\"touch\", \"/x\"]\n")
    assert profiles.cicustom("vendor=local:snippets/qga-vendor.yaml", profiles.load("bench")) == \
        "vendor=local:snippets/pvelab-bench.yaml"
    assert profiles.snippet_of({"cicustom": "vendor=local:snippets/pvelab-bench.yaml"}) == "pvelab-bench.yaml"
    assert profiles.snippet_of({"cicustom": "vendor=local:snippets/qga-vendor.yaml"}) is None


def clone_commands(monkeypatch, **kw):
    """The qm commands clone_vm runs (on_node faked: no host in them)."""
    seen = []
    monkeypatch.setattr(lifecycle, "run", lambda *a, **k: seen.append([str(x) for x in a]) or "")
    monkeypatch.setattr(lifecycle, "on_node", lambda node, *a: a)
    monkeypatch.setattr(lifecycle.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(lifecycle, "read_conf", lambda v: {"scsi0": "local-lvm:base-9000-disk-0/vm-301-disk-0,size=10G",
                                                           "cicustom": "vendor=local:snippets/qga-vendor.yaml"})
    monkeypatch.setattr(lifecycle.legacy, "write", lambda fields: seen.append(("record", fields)))
    for f, v in (("node_of", "pve1"), ("host_dns", "1.1.1.1"), ("ssh_keys", b""), ("ip_config", "ip=dhcp")):
        monkeypatch.setattr(lifecycle, f, lambda *a, v=v: v)
    lifecycle.clone_vm(301, "net95-user01", 9000, "95", "10.0.0.5", {}, "pw", 1, 512, 20, **kw)
    return seen


TODAY = [  # what clone_vm ran before profiles; without --profile it must stay exactly this
    ["qm", "clone", "9000", "301", "--name", "net95-user01"],
    ["qm", "set", "301", "--memory", "512", "--cores", "1", "--onboot", "1", "--net0", "virtio,bridge=vmbr95",
     "--ipconfig0", "ip=dhcp", "--ciuser", "user", "--cipassword", "pw", "--sshkeys", "/dev/stdin",
     "--ciupgrade", "0", "--nameserver", "1.1.1.1"],
    ["qm", "disk", "resize", "301", "scsi0", "20G"],
    ["qm", "snapshot", "301", "initial", "--description", "pvelab: before first boot"],
    ["qm", "start", "301"],
]


def test_without_a_profile_clone_runs_todays_commands(monkeypatch):
    seen = clone_commands(monkeypatch, user="user")
    (_, fields), = [s for s in seen if s[0] == "record"]
    assert "profile" not in fields[301]
    assert [s for s in seen if s[0] != "record"] == TODAY


def test_profile_is_set_before_the_initial_snapshot(monkeypatch, profile_dir):
    seen = clone_commands(monkeypatch, user="user", profile=profiles.load("bench"))
    (_, fields), = [s for s in seen if s[0] == "record"]
    assert fields[301]["profile"] == "bench"
    cmds = [s for s in seen if s[0] != "record"]
    assert cmds[:3] == TODAY[:3] and cmds[-2:] == TODAY[-2:]     # pvelab's own, then the profile's, then initial
    assert cmds[3] == ["qm", "set", "301", "--cpu", "host", "--balloon", "0", "--affinity", "0-3",
                       "--scsi2", "local-lvm:50", "--scsi3", "local-lvm:2",
                       "--cicustom", "vendor=local:snippets/pvelab-bench.yaml"]
    assert len(cmds) == 6


def test_profile_without_cloud_init_keeps_the_templates_vendor_data(monkeypatch, profile_dir):
    cmds = [s for s in clone_commands(monkeypatch, user="user", profile=profiles.load("plain")) if s[0] != "record"]
    assert cmds[3] == ["qm", "set", "301", "--numa", "1"]


def test_create_checks_profiles_first_and_hands_them_on(monkeypatch, profile_dir, capsys):
    made, checked, ensured, given = [], [], [], []
    create = fake_create(monkeypatch, made, checked)
    clone = create.clone_vm
    monkeypatch.setattr(create, "clone_vm", lambda *a, profile=None, **k: given.append(profile and profile.name)
                        or clone(*a, **k))
    monkeypatch.setattr(create.profiles, "ensure", lambda p, h: ensured.append((p.name, h)))
    with pytest.raises(LabError, match="nope"):
        create.cmd_create(["2x2", "debian13", "--net", "95", "--profile", "bench,nope"])
    assert made == []
    create.cmd_create(["2x2", "debian13", "--net", "95", "--profile", "bench,"])
    assert given == ["bench", None, "bench", None]
    assert len(ensured) == 1 and ensured[0][0] == "bench"
    out = capsys.readouterr().out
    assert "set profile bench on VM -1 before its first boot: cpu=host, balloon=0, affinity=0-3, 2 disks" in out


def test_space_check_counts_the_profile_disks():
    from pvelab import resources
    assert resources.check_resources(2, 0, None, None, (10, 30)) == []
    need = 2 * resources.EST_DISK_GB + 52
    assert f"~{need} GB may be needed" in resources.check_resources(2, 0, None, None, (10, 30), 52)[0]


def test_save_template_refuses_extra_disks(monkeypatch):
    conf = {"name": "net30-student01", "net0": "virtio=AA,bridge=vmbr30", "scsi0": "local-lvm:vm-201-disk-0,size=10G",
            "scsi1": "local-lvm:vm-201-cloudinit,media=cdrom", "scsi2": "local-lvm:vm-201-disk-1,size=50G"}
    monkeypatch.setattr(templates, "read_conf", lambda v: conf)
    monkeypatch.setattr(templates, "node_of", lambda v: None)
    monkeypatch.setattr(templates, "run", lambda *a, **k: pytest.fail(f"ran {a}"))
    with pytest.raises(LabError, match="scsi2"):
        templates.save_template(201, "bench-lab")
    del conf["scsi2"]
    assert templates.extra_disks({**conf, "ide2": "none,media=cdrom"}) == []


def test_move_copies_profile_snippets(monkeypatch, tmp_path):
    (tmp_path / "pvelab-bench.yaml").write_text("#cloud-config\n")
    monkeypatch.setattr(profiles, "SNIPPET_DIR", str(tmp_path))
    wrote = []
    monkeypatch.setattr(profiles, "write_on", lambda node, path, text: wrote.append((node, path, text)))
    confs = {301: {"_node": None, "cicustom": "vendor=local:snippets/pvelab-bench.yaml"},
             302: {"_node": None, "cicustom": "vendor=local:snippets/pvelab-bench.yaml"},
             303: {"_node": None, "cicustom": "vendor=local:snippets/pvelab-gone.yaml"},
             304: {"_node": None, "cicustom": "vendor=local:snippets/qga-vendor.yaml"}}
    assert profiles.copy_snippets(confs, "pve2") == ["pvelab-gone.yaml"]
    assert wrote == [("pve2", f"{tmp_path}/pvelab-bench.yaml", "#cloud-config\n")]
