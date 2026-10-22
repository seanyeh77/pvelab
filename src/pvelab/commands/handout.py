"""pvelab handout: a printable page with one login card per student."""

import html
import os
import time

from ..args import opt
from ..config import GUEST_USER
from ..credentials import cred_rows
from ..expose import is_exposed, public_host, ssh_port
from ..inventory import read_conf, targets
from ..isolation import student_of
from ..util import warn

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
  <div class="row">Password: <code>{password}</code></div>
  <div class="note">{note}Keep this password to yourself. Change it after logging in with <code>passwd</code>
  if you like (10+ characters).</div>
</div>"""


def cmd_handout(args):
    out, rest = None, []
    it = iter(args)
    for a in it:
        if (v := opt(a, it, "--output", "-o")) is not None:
            out = v
        else:
            rest.append(a)
    ids, rows, host = targets(rest), cred_rows(), public_host()
    nets = sorted({rows.get(i, {}).get("net", "dhcp") for i in ids})
    out = out or f"pvelab-handout-{'-'.join('net' + n for n in nets)}.html"
    closed = sorted({n for n in nets if n == "dhcp" or not is_exposed(n)})
    students = {}                             # a student with several VMs gets one card for all
    for i in ids:
        name = read_conf(i).get("name", str(i))
        students.setdefault(student_of(name), []).append((i, name))
    cards = []
    for student, vms in students.items():
        r = rows.get(vms[0][0], {})
        open_ = r.get("net") not in closed
        logins = []
        for i, name in vms:
            ssh = (f"ssh -p {ssh_port(i)} {rows.get(i, {}).get('user', GUEST_USER)}@{host}" if open_
                   else "(not reachable from outside yet)")
            label = f"{html.escape(name)}: " if len(vms) > 1 else "Log in: "
            logins.append(f'<div class="row">{label}<code>{html.escape(ssh)}</code></div>')
        note = "" if open_ else '<span class="warn">Ask your instructor to open ssh access. </span>'
        title = student + (f" ({len(vms)} VMs)" if len(vms) > 1 else "")
        cards.append(CARD.format(name=html.escape(title), logins="\n  ".join(logins),
                                 password=html.escape(r.get("password", "?")), note=note))
    title = f"VM logins - {', '.join('net' + n for n in nets)} - {time.strftime('%Y-%m-%d')}"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # it holds passwords
    with os.fdopen(fd, "w") as f:
        f.write(PAGE.format(title=html.escape(title), cards="\n".join(cards)))
    if os.environ.get("SUDO_UID"):            # made through sudo: let the person who asked open it
        os.chown(out, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", -1)))
    print(f"wrote {os.path.abspath(out)}: {len(cards)} card(s), {len(ids)} VM(s) - open it in a browser and print")
    print("  it contains the passwords: delete it after printing, don't share the file itself")
    for n in closed:
        warn(f"net{n}: ssh from outside is not open, so those cards have no ssh command"
             + ("" if n == "dhcp" else f" (pvelab expose --net {n})"))
