# pvelab move: moving students' VMs to another host

Date: 2026-11-01. Status: agreed in chat. "Just tell the user" about the costs.

## Why

With two hosts, the instructor wants to move VMs between them: one VM, one student's VMs, a whole class (`--net N`), or everything. Two reasons: to balance load and disk use, and to empty a host.

## Facts that shape it (checked in PVE 9.2 sources)

- **Snapshots on LVM-thin block a move.** PVE refuses to migrate a VM that has them (QemuMigrate.pm: "non-migratable snapshot exists"). Every pvelab VM has snapshots (`initial`, `clean`, script snapshots). So a move must delete the VM's snapshots first.
- **Linked clones become full copies.** On LVM-thin, PVE allows migrating a linked clone, but the moved disk becomes a full, independent copy on the target. The LvmThinPlugin.pm note: "increased disk usage when migrating LVM-thin volumes". The move needs about the VM's full disk data in free space on the target.
- **Running VMs.** With local disks and no snapshots, a running VM can be migrated online (`--online --with-local-disks`). pvelab still shuts the VM down and migrates it offline. That is simpler and safe, and it is the case PVE supports best.
- **Bridge nets stay put.** VMs on a bridge net (vmbrN, which includes net30 and net40 today) and VMs without `--net` (vmbr1) only have a network on their bridge's host, so they cannot move. Only SDN nets (labN) can.

## Goals

1. **The command:** `pvelab move VM... --to HOST [--dry-run]`.
   - VM is selected like every other command: a name, a range, a pattern in quotes (one student: `'net40-student05*'`), `--net N` (a whole class) or `all`.
   - HOST must be an online member of the cluster.
2. **A student's VMs always stay together.** If only some of a student's VMs are named, the plan adds the rest and says so. They go to the same host.
3. **Refused before anything changes, with the reason:**
   - VMs on a bridge or DHCP net
   - VMs already on HOST (skipped and listed; this is not an error if others still move)
   - templates, and VMs pvelab did not make
   - a target that is offline or is not in the cluster
   - not enough free space on the target's storage, using the space check pvelab already has
4. **One plan, one question.** Like create, it lists:
   - every VM and its current host
   - **the snapshots that will be deleted on each VM (named)**: they are lost, so there is no rollback to them afterwards
   - **the extra disk space on the target** (each VM's disk data, from lvs): moved VMs become full copies, not linked clones of the template
   - the VMs that will be shut down and started again (the running ones), and who is logged in to them (guest agent `who`)
   - that a new `clean` snapshot is taken on the target after the move

   Then it asks once. LAB_YES=1 skips the question, the same as other destructive commands; it uses the existing typed-yes confirm.
5. **A live board** shows one line per VM: shut down (if running) → delete snapshots → migrate (with the newest line of the `qm migrate` output as the live tail) → start (if it was running; wait for the guest agent) → snapshot `clean` → done.
   - VMs move one at a time. Migrations share the same disks and network, so this keeps it predictable.
   - A failure marks that VM, says which step failed and gives recovery advice, then goes on with the next student. It never leaves a student split: if one of a student's VMs fails, that student's remaining VMs are not moved.
6. **After the moves:**
   - refresh the per-host state of each affected net: isolation tables on each host, through the existing `refresh_net` / `set_isolation`
   - make sure the target host has the vendor-data snippet
   - print a summary (moved, skipped, failed) and the next steps (`pvelab list --net N`), and give the log path.
7. **Wizard:** a "Move VMs to another host" entry. It is shown only with 2 or more hosts. It picks the VMs (with the existing chooser), then the target host, then shows the plan.
8. **Help, completion and README:** completion offers the host names after `--to`.

## Non-goals

- Moving VMs on bridge nets (net30/net40). Converting a class to an SDN net is a separate feature.
- Keeping snapshots, and keeping linked clones.
- Live migration.
- Automatic rebalancing.

## Anti-goals

- **Splitting a student across hosts**, even when something fails.
- **Deleting snapshots without the plan having named them,** or without the instructor's yes.
- **Losing a VM.** pvelab runs `qm migrate` (the VM is gone from the source only once PVE finishes), never a manual copy plus delete.

## Testing

- Unit tests mock every host command:
  - the selection and grouping by student
  - each refusal
  - the plan text (snapshots named, space)
  - the step order on the board
  - a failure stopping that student
- **tests/selftest.sh, "two hosts" section:** move one student of a test SDN net to the other host, and check that:
  - the student's VMs are on the target
  - the student has a `clean` snapshot and no old snapshots
  - isolation still holds across the hosts
  - the moved VM reaches the internet.

  Then destroy as usual.

## Left to the builder

- The module layout (e.g. `moving.py` + `commands/move.py`).
- How the disk data per VM is measured (`templates.data_gb` already does it with lvs).
- Exact wording.
