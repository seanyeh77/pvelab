"""create --profile: VM settings from /etc/pve/pvelab/profiles/NAME.toml, set before the 'initial' snapshot.

    [qm]                  qm set options, allow-listed (QM_KEYS)
    cpu = "host"
    [[disk]]              an extra empty disk each: scsi2, scsi3, ... (scsi1 is the cloud-init drive)
    size = "50G"
    [cloud-init]          added to pvelab's vendor-data (CI_KEYS), as snippet local:snippets/pvelab-NAME.yaml
    runcmd = ["sed -i '/update_etc_hosts/d' /etc/cloud/cloud.cfg"]

All of it is in the config of 'initial' and 'clean', so a rollback keeps it; cloud-init runs the snippet at
every first boot (after rollback --initial too). Profiles are read when VMs are made only: a changed file
changes no VM that exists."""

import json
import os
import re
from dataclasses import dataclass

from .config import SNIPPET_DIR
from .helptext import bad_usage
from .nodes import is_local, run_on, write_on
from .util import LabError

try:
    import tomllib
except ImportError:                          # Python < 3.11 (pyproject asks for 3.11)
    tomllib = None

PROFILE_DIR = "/etc/pve/pvelab/profiles"     # cluster-wide, like admin_keys
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
QM_KEYS = {"cpu", "balloon", "affinity", "numa", "hugepages"}
RESERVED = {"memory", "cores", "net0", "scsi0", "ipconfig0", "ciuser", "cipassword", "sshkeys", "name", "onboot"}
CI_KEYS = {"bootcmd", "packages", "runcmd"}
FIRST_DISK, LAST_DISK = 2, 30                # scsi2 .. scsi30 (PVE has scsi0-30)
MAX_DISK_GB = 65536


@dataclass(frozen=True)
class Profile:
    name: str
    qm: tuple = ()           # ((key, value as qm takes it), ...) in the file's order
    disks: tuple = ()        # GB of each extra disk
    ci: tuple = ()           # ((key, (YAML items, ...)), ...)

    @property
    def snippet(self):
        """Its vendor-data snippet's file name in local:snippets, or None (no [cloud-init])."""
        return f"pvelab-{self.name}.yaml" if self.ci else None

    @property
    def disk_gb(self):
        return sum(self.disks)

    def qm_args(self, storage):
        """The qm set options of [qm] and [[disk]] (new disks on storage, as scsi0's)."""
        out = [x for k, v in self.qm for x in (f"--{k}", v)]
        for j, gb in enumerate(self.disks):
            out += [f"--scsi{FIRST_DISK + j}", f"{storage}:{gb}"]
        return out

    def summary(self):
        """'cpu=host, balloon=0, 1 disk (50 GB), cloud-init runcmd' for the plan."""
        parts = [f"{k}={v}" for k, v in self.qm]
        if self.disks:
            parts.append(f"{len(self.disks)} disk{'s' * (len(self.disks) > 1)} ("
                         + ", ".join(f"{g} GB" for g in self.disks) + ")")
        if self.ci:
            parts.append("cloud-init " + "+".join(k for k, _ in self.ci))
        return ", ".join(parts) or "nothing"


def path(name):
    return os.path.join(PROFILE_DIR, f"{name}.toml")


def names():
    """The profiles there are (completion, the menu); none when the directory can't be read."""
    try:
        return sorted(f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".toml") and NAME_RE.fullmatch(f[:-5]))
    except OSError:
        return []


def load(name):
    """Profile NAME from its file; a missing file or anything parse() refuses is a usage error."""
    if not NAME_RE.fullmatch(name):
        bad_usage(f"--profile {name}: a profile name is lowercase letters, digits, - and _ (at most 32)")
    if tomllib is None:
        bad_usage("--profile needs Python 3.11 or newer (tomllib)")
    try:
        with open(path(name), "rb") as f:
            text = f.read().decode()
    except FileNotFoundError:
        have = names()
        bad_usage(f"--profile {name}: no {path(name)}" + (f" (there are: {', '.join(have)})" if have else ""))
    except (OSError, UnicodeDecodeError) as e:
        bad_usage(f"--profile {name}: can't read {path(name)}: {e}")
    return parse(name, text)


def parse(name, text):
    """Profile NAME from TOML text; refuses unknown sections and keys, keys pvelab sets itself and bad values."""
    def no(why):
        bad_usage(f"--profile {name} ({path(name)}): {why}")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        no(f"not TOML: {e}")
    if extra := sorted(set(data) - {"qm", "disk", "cloud-init"}):
        no(f"unknown section {', '.join(extra)}: [qm], [[disk]] and [cloud-init] only")
    qm = []
    for k, v in table(data, "qm", no).items():
        if k in RESERVED:
            no(f"[qm] {k}: pvelab sets it itself" + (f" (create --{k})" if k in ("memory", "cores") else ""))
        if k not in QM_KEYS:
            no(f"[qm] {k}: not allowed (allowed: {', '.join(sorted(QM_KEYS))})")
        qm.append((k, qm_value(k, v, no)))
    disks = data.get("disk", [])
    if not isinstance(disks, list) or not all(isinstance(d, dict) for d in disks):
        no("disk: [[disk]] tables, each with size = \"50G\"")
    if len(disks) > LAST_DISK - FIRST_DISK + 1:
        no(f"at most {LAST_DISK - FIRST_DISK + 1} disks")
    gbs = []
    for d in disks:
        if extra := sorted(set(d) - {"size"}):
            no(f"[[disk]] {', '.join(extra)}: only size")
        gbs.append(disk_gb(d.get("size"), no))
    ci = []
    for k, v in table(data, "cloud-init", no).items():
        if k not in CI_KEYS:
            no(f"[cloud-init] {k}: not allowed (allowed: {', '.join(sorted(CI_KEYS))})")
        if not isinstance(v, list) or not v:
            no(f"[cloud-init] {k}: a list")
        ci.append((k, tuple(ci_item(k, x, no) for x in v)))
    return Profile(name, tuple(qm), tuple(gbs), tuple(ci))


def table(data, key, no):
    t = data.get(key, {})
    if not isinstance(t, dict):
        no(f"{key}: a [{key}] table")
    return t


def qm_value(k, v, no):
    """A [qm] value as qm set takes it, checked."""
    if isinstance(v, bool):
        v = int(v)
    s = str(v)
    ok = {"cpu": r"[A-Za-z0-9_.+=-]+(,[A-Za-z0-9_.+=;-]+)*",
          "balloon": r"\d{1,8}",
          "affinity": r"\d+(-\d+)?(,\d+(-\d+)?)*",
          "numa": r"[01]",
          "hugepages": r"any|2|1024"}[k]
    if isinstance(v, float) or not re.fullmatch(ok, s):
        no(f"[qm] {k} = {v!r}: not a value qm set --{k} takes")
    return s


def disk_gb(v, no):
    """[[disk]] size: '50G', '1T' or 50 (GB) -> GB."""
    m = re.fullmatch(r"(\d+)\s*([GgTt]?)", str(v)) if isinstance(v, (int, str)) and not isinstance(v, bool) else None
    gb = int(m.group(1)) * (1024 if m.group(2) in ("T", "t") else 1) if m else 0
    if not 1 <= gb <= MAX_DISK_GB:
        no(f"[[disk]] size = {v!r}: GB like \"50G\" (1 GB to 64 TB)")
    return gb


def ci_item(k, x, no):
    """One [cloud-init] list item as YAML: a string, or (runcmd, bootcmd) a list of strings (argv)."""
    if isinstance(x, str) and "\0" not in x:
        return json.dumps(x)                 # JSON strings are YAML double-quoted scalars
    if k != "packages" and isinstance(x, list) and x and all(isinstance(a, str) for a in x):
        return "[" + ", ".join(map(json.dumps, x)) + "]"
    no(f"[cloud-init] {k}: strings" + (" or lists of strings" if k != "packages" else ""))


def per_vm_arg(v):
    """One VM's --profile value: a Profile, or None (empty: no profile). Each name is read once per run."""
    if not v:
        return None
    if v not in _loaded:
        _loaded[v] = load(v)
    return _loaded[v]


_loaded = {}


def snippet_text(p):
    from .templates import vendor_data       # templates imports lifecycle, which create imports too
    return vendor_data(dict(p.ci))


def ensure(p, node=None):
    """Profile p's vendor-data snippet on node (its VMs' qm start needs it there); nothing without [cloud-init]."""
    if p and p.snippet:
        write_on(node, f"{SNIPPET_DIR}/{p.snippet}", snippet_text(p))


def cicustom(cur, p):
    """A VM's cicustom (cur, from its template: vendor=pvelab's snippet) with vendor= profile p's snippet."""
    keep = [x for x in str(cur or "").split(",") if x and not x.startswith("vendor=")]
    return ",".join([f"vendor=local:snippets/{p.snippet}"] + keep)


def snippet_of(conf):
    """The profile snippet (pvelab-NAME.yaml) a VM config's cloud-init uses, or None."""
    for x in str((conf or {}).get("cicustom") or "").split(","):
        if m := re.fullmatch(r"vendor=local:snippets/(pvelab-[a-z0-9_-]+\.yaml)", x.strip()):
            return m.group(1)
    return None


def copy_snippets(confs, to):
    """move: the profile snippets of the VMs confs ({vmid: conf with _node}) copied from their hosts to host
    to, unless there already; their cloud-init needs them there. The names of those that could not be read."""
    done, lost = set(), []
    for c in confs.values():
        s = snippet_of(c)
        if not s or s in done:
            continue
        done.add(s)
        f = f"{SNIPPET_DIR}/{s}"
        src = c.get("_node")
        try:
            text = open(f).read() if is_local(src) else run_on(src, "cat", f, quiet=True)
        except (OSError, LabError):          # not there (here, or over ssh)
            text = None
        if text:
            write_on(to, f, text)
        else:
            lost.append(s)
    return lost
