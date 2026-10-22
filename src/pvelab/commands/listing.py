"""pvelab list"""

from ..blocks import block_label, blocks_load
from ..config import DHCP_BRIDGE, GUEST_USER, STORAGE
from ..credentials import cred_rows
from ..expose import is_exposed, public_host, ssh_port
from ..helptext import bad_usage
from ..inventory import desc_value, managed
from ..args import opt, valid_net
from ..networks import net_label, vm_ip
from ..resources import size_label, storage
from ..templates import template_label
from ..util import run, warn


def cmd_list(args):
    ssh_only, only_net = False, None
    it = iter(args)
    for a in it:
        if a == "--ssh":
            ssh_only = True
        elif (v := opt(a, it, "--net")) is not None:
            only_net = v
        else:
            bad_usage(f"unknown option '{a}'")
    if only_net is not None:
        only_net = "dhcp" if only_net == "dhcp" else valid_net(only_net)
    rows = cred_rows()
    vms = {i: c for i, c in managed().items()
           if only_net is None or rows.get(i, {}).get("net", desc_value(c, "net") or "dhcp") == only_net}
    exposed = {n for n in {r["net"] for r in rows.values()} if n != "dhcp" and is_exposed(n)}
    if ssh_only:
        return list_ssh(vms, rows, exposed, only_net)
    status = {int(l.split()[0]): l.split()[2] for l in run("qm", "list").splitlines()[1:] if l.split()}
    blocks = blocks_load()
    fmt = "{:<18} {:<6} {:<18} {:<11} {:<9} {:<12} {:<15} {:<6} {:<13} {:<9} {}"
    print(fmt.format("NAME", "VMID", "TEMPLATE", "NET", "STATUS", "SIZE", "IP", "SSH", "BLOCKED", "USER",
                     "PASSWORD"))
    for vmid, c in vms.items():
        r = rows.get(vmid, {})
        tpl = desc_value(c, "template")
        print(fmt.format(c.get("name", ""), vmid, template_label(int(tpl)) if tpl.isdigit() else "?",
                         net_label(r.get("net", "dhcp")), status.get(vmid, "?"), size_label(c),
                         vm_ip(vmid, rows),
                         ssh_port(vmid) if r.get("net") in exposed else "-", block_label(vmid, blocks),
                         r.get("user", "?"),
                         r.get("password", "")))
    if exposed & {rows.get(i, {}).get("net") for i in vms}:
        print(f"\nstudents: ssh -p <SSH> <USER>@{public_host()}   (pvelab list --ssh for the full commands)")
    pct, free = storage()
    print(f"\n{STORAGE}: {pct if pct is not None else '?'}% used, {free} GB free")
    if pct is not None and pct >= 80:
        warn(f"{STORAGE} is over 80% full")


def list_ssh(vms, rows, exposed, only_net):
    """One ready-to-send line per student: name, ssh command, password."""
    host, shown, closed = public_host(), 0, set()
    fmt = "{:<18} {:<40} {}"
    print(fmt.format("NAME", "SSH", "PASSWORD"))
    for vmid, c in vms.items():
        r = rows.get(vmid, {})
        if r.get("net") not in exposed:
            closed.add(r.get("net", "dhcp"))
            continue
        print(fmt.format(c.get("name", ""), f"ssh -p {ssh_port(vmid)} {r.get('user', GUEST_USER)}@{host}",
                         r.get("password", "")))
        shown += 1
    if not shown:
        print("(none)")
    for n in sorted(closed - {"dhcp"}, key=int):
        print(f"\nnet{n} is not exposed - open it with: pvelab expose --net {n}")
    if "dhcp" in closed:
        print(f"\nVMs on {DHCP_BRIDGE}(dhcp) can't be exposed; create them with --net N instead")
