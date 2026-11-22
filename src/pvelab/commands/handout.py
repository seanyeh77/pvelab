"""pvelab handout: a printable page with one login card per owner."""

import html
import os
import time

from ..args import opt
from ..config import GUEST_USER, is_external, is_plain
from ..credentials import cred_rows
from ..expose import forwarded_on, is_exposed, public_host, ssh_command
from ..inventory import net_of, sorted_nets, targets, vm_names
from ..owners import confs_of, group_login, groups_of, is_shared_vm
from ..nets import net_name
from ..networks import vm_ip
from ..util import parallel, warn, write_file

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font: 14px/1.4 system-ui, sans-serif; margin: 16px; color: #111; background: #fff; }}
  h1 {{ font-size: 18px; margin: 0 0 12px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }}
  .card {{ border: 1px dashed #888; border-radius: 6px; padding: 12px; break-inside: avoid; }}
  .name {{ font-weight: 600; font-size: 16px; }}
  code {{ font: 13px ui-monospace, monospace; background: #f2f2f2; padding: 2px 4px; border-radius: 3px;
          word-break: break-all; }}
  .row {{ margin-top: 6px; }}
  .note {{ color: #555; font-size: 12px; margin-top: 8px; }}
  .warn {{ color: #a00; }}
  @media print {{ body {{ margin: 0; }} h1 {{ display: none; }} }}
</style></head><body>
<h1>{title}</h1>
<div class="cards">
{cards}
</div>
</body></html>
"""

CARD = """<div class="card">
  <div class="name">{name}</div>
  {logins}
  <div class="row">User: <code>{user}</code></div>
  <div class="row">Password: <code>{password}</code></div>
  <div class="note">{note}Keep this password to yourself. Change it after logging in with <code>passwd</code>
  if you like (choose a strong one).</div>
</div>"""


def cmd_handout(args):
    out, rest = None, []
    it = iter(args)
    for a in it:
        if (v := opt(a, it, "--output", "-o")) is not None:
            out = v
        else:
            rest.append(a)
    ids, rows = targets(rest), cred_rows()
    vms = {i: rows.get(i, {}) for i in ids}
    confs = confs_of(vms)
    net, names = {i: net_of(i, rows, confs[i]) for i in ids}, vm_names(ids, confs)
    shared = [i for i in ids if is_shared_vm(confs[i], net[i])]
    if shared and len(shared) < len(ids):    # its password is the admin's: no card unless asked alone
        ids = [i for i in ids if i not in shared]
        print("left out (a shared VM; for its card: pvelab handout " + " ".join(names[i] for i in shared) + ")")
    on = {net[i] for i in ids}
    nets = sorted_nets(on) + (["dhcp"] if "dhcp" in on else [])
    out = out or f"pvelab-handout-{'-'.join(n if is_plain(n) else 'net' + n for n in nets)}.html"
    closed = [n for n in nets if n == "dhcp" or not is_plain(n) and not is_exposed(n)]
    outside = [i for i in ids if is_external(net[i])]     # an admin's bridge: ssh to the VM's own address
    ips = {i: "" if ip == "-" else ip      # "-": no address known (stopped)
           for i, ip in (parallel(lambda i: vm_ip(i, rows), outside) if outside else {}).items()}
    fwd = forwarded_on(nets)                     # VMs whose ssh port is forwarded
    host = public_host() if fwd else None        # only cards with an ssh command need it
    cards = []
    for (_, owner), vm_ids in groups_of({i: vms[i] for i in ids}, confs).items():    # an owner's VMs on a net: one card
        user, pw = group_login(vms, vm_ids)
        open_ = all(i in fwd or ips.get(i) for i in vm_ids)
        logins = []
        for i in vm_ids:
            ssh = (ssh_command(i, vms[i], host) if i in fwd
                   else f"ssh {vms[i].get('user', GUEST_USER)}@{ips[i]}" if ips.get(i)
                   else "(no address yet: start it, then print again)" if i in ips
                   else "(not reachable from outside yet)")
            label = f"{html.escape(names[i])}: " if len(vm_ids) > 1 else "Log in: "
            logins.append(f'<div class="row">{label}<code>{html.escape(ssh)}</code></div>')
        note = "" if open_ else '<span class="warn">SSH access is not open yet: ask the admin. </span>'
        title = (names[vm_ids[0]] if len(vm_ids) == 1 else f"{owner} ({len(vm_ids)} VMs)")
        cards.append(CARD.format(name=html.escape(title), logins="\n  ".join(logins), user=html.escape(user or "?"),
                                 password=html.escape(pw or "-"), note=note))
    title = f"VM logins - {', '.join(map(net_name, nets))} - {time.strftime('%Y-%m-%d')}"
    write_file(out, PAGE.format(title=html.escape(title), cards="\n".join(cards)), 0o600)   # it holds passwords
    if os.environ.get("SUDO_UID"):            # made through sudo: let the person who asked open it
        os.chown(out, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", -1)))
    print(f"wrote {os.path.abspath(out)}: {len(cards)} card(s), {len(ids)} VM(s) - open it in a browser and print")
    print("  it contains the passwords: delete it after printing, don't share the file itself")
    for n in closed:
        warn(f"{net_name(n)}: ssh from outside is not open, so those cards have no ssh command"
             + ("" if n == "dhcp" else f" (pvelab expose --net {net_name(n)})"))
