"""Unit tests for the parts of pvelab that need no PVE host."""
import pytest

from pvelab.catalogue import TEMPLATES
from pvelab.config import PRE
from pvelab.helptext import USAGE, command_help
from pvelab.args import valid_net
from pvelab.resources import conf_disk_gb, parse_cores, parse_disk, parse_mem, size_label
from pvelab.scripts import script_snapshot_name
from pvelab.templates import catalogue_find
from pvelab.util import LabError


@pytest.mark.parametrize("arg, mb", [("2048", 2048), ("4G", 4096), ("4g", 4096), ("512M", 512)])
def test_parse_mem(arg, mb):
    assert parse_mem(arg) == mb


@pytest.mark.parametrize("arg", ["", None, "abc", "100", "1.5G", "-1"])
def test_parse_mem_rejects(arg):
    with pytest.raises(LabError):
        parse_mem(arg)


def test_parse_cores():
    assert parse_cores("4") == 4
    for bad in ("0", "65", "x", None):
        with pytest.raises(LabError):
            parse_cores(bad)


@pytest.mark.parametrize("arg, out", [("30", (False, 30)), ("30G", (False, 30)), ("+10", (True, 10))])
def test_parse_disk(arg, out):
    assert parse_disk(arg) == out


@pytest.mark.parametrize("arg", ["0", "-5", "ten", "", None])
def test_parse_disk_rejects(arg):
    with pytest.raises(LabError):
        parse_disk(arg)


def test_disk_and_size_label():
    conf = {"cores": "2", "memory": "1536", "scsi0": "local-lvm:vm-201-disk-0,size=12G"}
    assert conf_disk_gb(conf) == 12
    assert conf_disk_gb({"scsi0": "x,size=2048M"}) == 2
    assert conf_disk_gb({}) == 0
    assert size_label(conf) == "2c/1.5G/12G"
    assert size_label({"cores": "2", "memory": "2048", "scsi0": "x,size=10G"}) == "2c/2G/10G"


@pytest.mark.parametrize("path, snap", [
    ("setup.sh", "setup"), ("/tmp/course-setup.sh", "course-setup"),
    ("01-init.sh", "s-01-init"), ("my script.py", "my-script"),
])
def test_script_snapshot_name(path, snap):
    assert script_snapshot_name(path) == snap


def test_script_snapshot_name_fits_before_prefix():
    name = script_snapshot_name("a" * 80 + ".sh")
    assert len(PRE + name) <= 40              # PVE's limit for snapshot names


@pytest.mark.parametrize("path", ["clean.sh", "initial.sh", "current.sh", "before-x.sh", "x.sh"])
def test_script_snapshot_name_reserved(path):
    with pytest.raises(LabError):
        script_snapshot_name(path)


def test_valid_net():
    assert valid_net("40") == "40"
    for bad in ("1", "10", "255", "x"):
        with pytest.raises(LabError):
            valid_net(bad)


def test_catalogue():
    assert len({t.vmid for t in TEMPLATES}) == len(TEMPLATES)
    aliases = [a for t in TEMPLATES for a in t.aliases]
    assert len(set(aliases)) == len(aliases)
    assert catalogue_find("ubuntu24").vmid == 9004
    assert catalogue_find("9002").name == "debian-13-cloud"
    assert catalogue_find("nope") is None


@pytest.mark.parametrize("cmd", ["create", "list", "power", "destroy", "rollback", "run", "snapshot",
                                 "expose", "block", "resize", "upgrade", "ssh", "templates", "pull"])
def test_every_command_has_help(cmd):
    assert command_help(cmd).startswith("usage: pvelab")


def test_usage_lists_commands():
    for cmd in ("create", "list", "destroy", "rollback", "run", "expose", "block", "resize", "upgrade"):
        assert f"  {cmd}" in USAGE


def test_net_rules_only_new_outgoing_connections():
    from pvelab.blocks import net_rules
    rules = net_rules("30")
    assert len(rules) == 2 and all("-i vmbr30 -o vmbr0" in r and "--ctstate NEW -j DROP" in r for r in rules)
    assert "3333" in rules[0] and len(rules[0].split("--dports ")[1].split()[0].split(",")) <= 15


def test_vendor_data_hardens_ssh_and_passwords():
    from pvelab.templates import vendor_data
    v = vendor_data()
    assert v.startswith("#cloud-config\n") and "AllowUsers student\\n" in v
    assert "libpam-pwquality" in v and "enforce_for_root" in v


def test_targets_net_forms(monkeypatch):
    from pvelab import inventory
    man = {201: {"name": "net30-student01", "description": "net=30"},
           202: {"name": "net30-student02", "description": "net=30"},
           301: {"name": "net40-student01", "description": "net=40"}}
    monkeypatch.setattr(inventory, "managed", lambda: man)
    monkeypatch.setattr(inventory, "cred_rows", lambda: {})
    assert inventory.targets(["--net", "30"]) == [201, 202]
    assert inventory.targets(["--net=30"]) == [201, 202]
    assert inventory.targets(["--net=40", "net30-student02"]) == [202, 301]
    with pytest.raises(LabError, match="--net 30"):
        inventory.targets(["net30"])
    with pytest.raises(LabError):
        inventory.targets(["--net"])


def test_net_forms_agree_across_commands(monkeypatch):
    from pvelab import inventory
    monkeypatch.setattr(inventory, "managed", lambda: {201: {"name": "net30-student01", "description": "net=30"}})
    monkeypatch.setattr(inventory, "cred_rows", lambda: {})
    assert inventory.targets(["--net", "030"]) == inventory.targets(["--net=net30"]) == [201]
    assert valid_net("net30") == "30"


def test_opt_forms():
    from pvelab.args import opt
    it = iter(["4G", "rest"])
    assert opt("--memory", it, "--memory") == "4G" and next(it) == "rest"
    assert opt("--memory=4G", iter([]), "--memory") == "4G"
    assert opt("-t", iter(["ubuntu26"]), "--template", "-t") == "ubuntu26"
    assert opt("--template=debian13", iter([]), "--template", "-t") == "debian13"
    assert opt("--memoryx=1", iter([]), "--memory") is None and opt("--reboot", iter([]), "--memory") is None
    for a, rest in (("--to", []), ("--to=", [])):
        with pytest.raises(LabError, match="--to needs a value"):
            opt(a, iter(rest), "--to")
