# ================================================================ VM inventory
def read_conf(vmid):
    """Main section of a VM config: dict of keys + 'description' (decoded), or None."""
    d = {"description": ""}
    desc = []
    try:
        lines = open(f"{CONF_DIR}/{vmid}.conf").read().splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if line.startswith("["):              # snapshots/pending sections
            break
        if line.startswith("#"):
            desc.append(urllib.parse.unquote(line[1:]))
            continue
        k, _, v = line.partition(": ")
        d[k] = v
    d["description"] = "\n".join(desc)
    return d


def inventory():
    """{vmid: conf} for every VM on this node."""
    out = {}
    for f in glob.glob(f"{CONF_DIR}/*.conf"):
        vmid = int(os.path.basename(f)[:-5])
        out[vmid] = read_conf(vmid)
    return dict(sorted(out.items()))


def is_managed(conf):
    if not conf or conf.get("template") == "1":
        return False
    tags = re.split(r"[;, ]+", conf.get("tags", ""))
    return TAG in tags and MARKER in conf["description"]


def managed():
    return {i: c for i, c in inventory().items() if is_managed(c)}


def vm_exists(vmid):
    """VMID in use anywhere in the cluster (VM or container)."""
    return bool(glob.glob(f"/etc/pve/nodes/*/qemu-server/{vmid}.conf")
                or glob.glob(f"/etc/pve/nodes/*/lxc/{vmid}.conf"))


def vm_status(vmid):
    return run("qm", "status", vmid).split()[-1]


def resolve(name):
    inv = inventory()
    for i, c in inv.items():
        if c.get("name") == name and is_managed(c):
            return i
    if any(c.get("name") == name for c in inv.values()):
        die(f"'{name}' exists but was not created by lab - refusing to touch it")
    die(f"no student VM named '{name}'")


def targets(args):
    """<name|netN|pattern|all>... -> VMIDs of managed VMs, in VMID order."""
    if not args:
        bad_usage("which VM(s)?")
    man, rows, out = managed(), cred_rows(), set()
    for a in args:
        if a == "all":
            out |= set(man)
        elif any(c.get("name") == a for c in man.values()):
            out |= {i for i, c in man.items() if c.get("name") == a}
        elif re.fullmatch(r"net\d+", a):
            n = a[3:]
            ids = {i for i, c in man.items() if rows.get(i, {}).get("net", desc_value(c, "net")) == n}
            if not ids:
                die(f"no student VMs in {a}")
            out |= ids
        elif any(ch in a for ch in "*?["):
            ids = {i for i, c in man.items() if fnmatch.fnmatchcase(c.get("name", ""), a)}
            if not ids:
                die(f"no student VM matches '{a}'")
            out |= ids
        else:
            out.add(resolve(a))              # explains unknown / not-ours names
    return sorted(out)


def desc_value(conf, key):
    m = re.search(rf"{key}=(\S+)", conf["description"])
    return m.group(1) if m else ""


