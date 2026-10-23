# pvelab

Classroom VMs on one Proxmox VE node: one command makes a VM (or several) per student, on its own NAT net.

## Install

```bash
curl -fsSL https://seanyeh77.github.io/pvelab/install.sh | bash    # -> ~/.local/bin/pvelab, uses sudo itself
pvelab              # step-by-step menu
pvelab help [CMD]   # every command, with examples
pvelab upgrade
```

## Features

- **Create**: Debian/Ubuntu cloud images, one password per student, snapshots `initial` and `clean`.
- **Several VMs per student**: `20x3` gives `net40-student01-1..3`.
- **Isolation**: on by default. Each student reaches only their own VMs; `--no-isolate` is for group work.
- **ssh from outside**: `--expose` opens port 22000+VMID on the host.
- **Hardening**: only the `student` user may ssh in; passwords need 10+ characters; mining-pool ports are blocked.
- **Class control**: start/stop, roll back, run a script as root in every VM, block internet/ssh, resize.
- **Watching**: `list` (status, IPs, ports) and `top` (CPU/memory/network; flags busy VMs).
- **Handout**: printable login cards.
- Safe to try: `--dry-run` for destroy/resize/rollback, bash tab completion.

## Usage

```bash
pvelab create 20x2 ubuntu24 --net 40 --cores 4 --memory 8G --disk 30 --expose --script setup.sh
pvelab list --ssh
pvelab handout --net 40                       # print, hand out, delete the file
pvelab run --net 40 lab2.sh                   # run a script as root in every VM
pvelab block internet --net 40                # exam time; unblock internet --net 40
pvelab rollback 'net40-student0*' --to clean  # undo a student's changes
pvelab resize net40-student01..05 --memory 16G --reboot
pvelab top --check
pvelab destroy --net 40                       # end of term
```

To pick VMs, use one of:
- a name: `net40-student01-2`
- a whole net: `--net 40`
- a range: `net40-student01..05`
- a pattern: `'net40-student*-1'`
- `all`

## Develop

```bash
PYTHONPATH=src python3 -m pvelab help    # run from the checkout
pyflakes src tests && pytest             # unit tests, no PVE needed
python3 build.py                         # -> dist/pvelab (one-file zip app)
sudo tests/selftest.sh                   # end-to-end on the PVE host (net91/92/94, ~20 min)
git push                                 # CI publishes to GitHub Pages; hosts: pvelab upgrade
```

Code: `src/pvelab/` (one module per area, `commands/` one per command).
