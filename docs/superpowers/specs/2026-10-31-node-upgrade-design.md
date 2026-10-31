# pvelab node upgrade: update PVE on every host, major versions included

Date: 2026-10-31. Status: agreed in chat. Reboots are allowed.

## Why

The instructor wants one command that brings every PVE host of the cluster to the newest version. That covers both:
- updates within a major version (9.2 to 9.x)
- a new major version, for example 9 to 10 when it exists. A new major version moves the Debian release as well, for example trixie to forky.

Today both hosts, workshops (10.0.0.123) and pve9 (10.0.0.229), run PVE 9.2 on trixie with the pve-no-subscription repository. The sources are in deb822 files under `/etc/apt/sources.list.d/` (debian.sources, proxmox.sources, plus disabled ceph and enterprise files). PVE 10 does not exist yet, so the major-upgrade path cannot be tried on a real host. It must be written from PVE's documented procedure and its checker script. PVE 9 ships `pve8to9`, and a later PVE 9 will ship `pve9to10`.

## Goals

1. **`pvelab node upgrade [HOST...] [--major] [--dry-run]`**, run on workshops. Without HOST it works on every host, one at a time.
2. **Read-only checks first.** It shows each host's state and then asks once, after showing the plan in the usual pvelab way. The checks are:
   - every host is online and the cluster is quorate
   - free space on `/` and `/boot`
   - `apt-get update` works
   - what would be upgraded: the number of packages, and whether the kernel or pve-manager is among them
   - for `--major`:
     - PVE's checker for this version (`pveXtoY --full`) must pass with no FAILURES
     - the target release must be published, both PVE's repository for it and Debian's suite.

     If the next major version does not exist, it says so and stops.
3. **Rolling, one host at a time.** This keeps the cluster up. For each host:
   1. Show who is logged in to its students' VMs, using the guest agent's `who`. If anyone is, warn and ask (the default answer is no). LAB_YES=1 skips the question.
   2. With `--major`, rewrite the apt sources to the new suites:
      - the Debian suites, plus `-updates` and `-security`
      - the PVE repository's suite
      - Ceph's, only if it is enabled

      The old files are backed up next to the new ones (`*.pvelab-bak`).
   3. Run `apt-get update`, then a non-interactive `apt-get dist-upgrade`:
      - DEBIAN_FRONTEND=noninteractive, with `--force-confdef` and `--force-confold`
      - output into the log; the board shows the newest apt line live, as the other commands do.
   4. Reboot, if a new kernel was installed or the upgrade was a major one. Otherwise only if `/run/reboot-required` exists.
      - **When the host is workshops**, pvelab itself runs there, so the reboot ends pvelab. Before rebooting, it prints exactly how to continue, for example `pvelab node upgrade` again, which skips hosts that are already current. It reboots workshops last.
   5. Wait for the host to come back: online, quorate, `pveversion` showing the new version. The VMs set to onboot=1 start by themselves. Then go to the next host.
4. **With two hosts, warn first.** While one host reboots, the other loses quorum: `/etc/pve` is read-only, and running VMs keep running. A QDevice would avoid this. Say so before starting.
5. **Rerunnable.** A host that is already on the newest packages, or already on the target major version, is skipped.
6. **On failure:** stop. Say which host and which step failed, what state each host is in, and how to recover, for example to restore the `*.pvelab-bak` sources and run `apt-get -f install`. Leave the other hosts alone.
7. **One log per run** in `/root/lab/logs/`, and a summary at the end: the version of each host before and after.

## Non-goals

- Upgrading the students' VMs, which run Debian or Ubuntu.
- Migrating VMs between hosts during the upgrade. Disks are local LVM-thin, so their VMs simply restart with the host.
- Ceph.
- Unattended or scheduled upgrades.

## Anti-goals

- A half-upgraded host that pvelab reports as fine.
- Changing repositories without a backup.
- Rebooting a host while students are logged in, without the instructor's yes.
- Running a major upgrade while PVE's checker reports failures.

## Constraints

- Commands on another host run through `nodes.run_on` (the cluster's root ssh). Long ones need a timeout and their output in the log.
- Reuse the existing helpers: `ui.Board`, `ui.Spinner`, `ui.Plan`, `ui.start_log`, `joining.wait_for`-style waits, and `members()`.
- **Tests:**
  - Unit tests mock every host command. A conftest guard already blocks real host commands.
  - Nothing is run on the real hosts during development.

## Left to the builder

- The module layout.
- How a new major version and its suites are discovered: PVE's repository index and Debian's Release files.
- Exact wording.
