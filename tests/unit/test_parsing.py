"""Unit tests for the parts of pvelab that need no PVE host."""
import pytest

from pvelab.catalogue import TEMPLATES
from pvelab.config import PRE
from pvelab.helptext import USAGE, command_help
from pvelab.networks import valid_net
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
