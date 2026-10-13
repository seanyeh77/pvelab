# pvelab

Student VMs on a Proxmox VE node: clone from cloud-image templates, isolated NAT nets,
ssh from outside, snapshots/rollback, instructor scripts, internet/ssh blocking.

## Install (on the PVE host)

```bash
curl -fsSL https://seanyeh77.github.io/pvelab/install.sh | bash
pvelab              # usage
pvelab upgrade      # later: download the latest version
```

Root gets `/usr/local/bin/pvelab`, other users `~/.local/bin/pvelab` (pvelab asks before using sudo).

## Layout

| path | |
|---|---|
| `src/*.py` | pvelab, cut into parts; joined in file-name order |
| `src/10-catalogue.py` | OS templates - add a Debian/Ubuntu release here |
| `build.py` | joins `src/` into the single file `dist/pvelab` |
| `install.sh` | the installer served by GitHub Pages |
| `tests/selftest.sh` | end-to-end test on a real PVE host (makes and destroys net91/92 VMs) |
| `.github/workflows/pages.yml` | on push to main: build, publish `pvelab` + `install.sh` |

The parts in `src/` are not modules: they are one script in pieces, so any part can use
anything from another. Shipping one file keeps install and `pvelab upgrade` simple.

## Develop

```bash
python3 build.py                    # -> dist/pvelab
sudo tests/selftest.sh              # ~20 min, on the PVE host
git push                            # GitHub Pages publishes it; hosts update with: pvelab upgrade
```
