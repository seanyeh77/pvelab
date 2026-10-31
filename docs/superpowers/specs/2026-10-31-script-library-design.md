# pvelab script library — design

Date: 2026-10-31. Status: design approved in chat; review questions answered (below).

## Why

- Every VM made from a pvelab template gets cloud-init vendor-data at first boot. Today that vendor-data also writes two restrictions:
  - `/etc/ssh/sshd_config.d/10-pvelab.conf` with `AllowUsers student`.
  - A pwquality rule: passwords need at least 10 characters, and the rule applies to root too.
- These restrictions get in the way of non-class VMs. For example, the instructor is setting up a service VM ("wg") that has to accept LDAP users over ssh.
- They also apply to VMs cloned by hand from a pvelab template, because `cicustom` is copied with the template.
- The instructor wants:
  - Templates that stay clean: they only make a cloud image usable, and they restrict nothing.
  - Anything else done after boot, by scripts.
  - A script library shipped with pvelab that they can browse and pick from, both when creating a class and later.

## Goals

1. **Vendor-data: only three relaxations.** The vendor-data shared by all templates does only these:
   - `ssh_pwauth: true`
   - install `qemu-guest-agent`
   - `package_update: true`

   It drops `AllowUsers` and pwquality, and drops `libpam-pwquality` from the package list.
2. **Script library in the repo.** A top-level `scripts/` folder in the pvelab repo, with one script per file. `build.py` packs it into `dist/pvelab`, so `pvelab upgrade` delivers new and changed scripts.
3. **`pvelab scripts` lists the library.**
   - With no argument, it shows name and description.
   - `pvelab scripts NAME` prints the script.
4. **`create --script` accepts several scripts, per VM.**
   - The value is a per-VM list, in the same style as TEMPLATE, `--cores`, `--memory` and `--disk`.
   - A comma separates a student's VMs (the k-th entry applies to every student's VM `-k`).
   - `+` chains several scripts on one VM, run in order.
   - An empty entry means no script for that VM. Example: `--script harden+docker,` runs nothing on `-2`.
   - Every student gets the same mapping.
5. **`run` accepts the same chains:** `pvelab run VM... harden+docker`.
6. **One snapshot pair per command, not per script.** Details are under "Snapshots".
7. **Library names and file paths both work.** Each entry in a chain is either a library name or a path to a file. The current `--script ./setup.sh` keeps working.
8. **CI enforces script headers.**
   - Every `scripts/*.sh` must have the `#!` shebang on line 1.
   - Line 2 must be a non-empty English description comment: `# <description>`.
9. **The wizard asks for scripts by VM.**
   - `create` asks which scripts each of a student's VMs should run.
   - `run` asks which scripts to run.
   - Both show the library list, and a file path is also accepted.
10. **Completion.** `--script` and `run` complete library names.

## Non-goals

- **No fetching scripts from GitHub at run time.** Scripts change only through `pvelab upgrade` (approved). Reasons:
  - No network dependency during class.
  - One pvelab version always means the same script contents.
  - One trusted delivery channel for code that runs as root in VMs.
- **No local or per-node library folder** (e.g. `/etc/pve/pvelab/scripts`). It was considered and dropped. Ad-hoc scripts are run by path instead.
- **No change to host network rules:** net separation, mining-port drops, ssh-only DNAT, isolate and block stay as they are.
- **No change to VMs that already exist** (net30, net40).
  - Their cloud-init already ran.
  - PVE's NoCloud instance-id is `sha1(user-data + network-config)` and does not include vendor-data, so changing the snippet does not make cloud-init run again on them.
  - Their existing `10-pvelab.conf` stays. Removing it is a separate, manual decision.
- **No automatic `pvelab upgrade`.**
- **No change to `save-template`** (instructor's decision). A saved template is exactly what its source VM was. If the source VM has `10-pvelab.conf` or the pwquality file, from the old vendor-data or from `harden`, the saved template and its clones keep them. `FORGET` is not changed.

## Anti-goals (failures even if it "works")

- **Templates or their clones still carry a restriction.** This covers clones made by hand in the PVE UI.
- **Losing the current safety of `run`:**
  - before/after snapshots
  - per-VM logs
  - parallel execution across VMs
  - the 1 MiB check
  - the `#!` check
- **Ambiguous behaviour when a name could be a library script or a file.** The output must say which one was used.

## Design

### Repo layout and packaging
```
scripts/
  docker.sh     # from ~/docker.sh (Docker Engine + compose; student may use docker without sudo)
  harden.sh     # the removed restrictions: AllowUsers student + pwquality minlen 10 enforce_for_root
```
- **Name.** The script's name is the file name without `.sh`. It must match `[a-z][a-z0-9-]*`.
- **Description.** The description is line 2, with the leading `# ` removed.
- **Packing.** `build.py` adds `scripts/*.sh` to the zip app, for example under `pvelab/library/`. The script bytes are included in the version hash, so a change to a script alone produces a new version and `pvelab upgrade` replaces the file.
- **Runtime lookup.** At run time, pvelab reads the library from the zip, through the package's loader or importlib.resources. When pvelab runs from source (`PYTHONPATH=src`, tests), it falls back to `<repo>/scripts/`.

### harden.sh contents
- It recreates exactly what the vendor-data did before:
  - write `/etc/ssh/sshd_config.d/10-pvelab.conf`: `AllowUsers student`
  - install `libpam-pwquality`
  - write `/etc/security/pwquality.conf.d/pvelab.conf`: `minlen = 10`, `enforce_for_root`
  - reload ssh
- It must be safe to run twice.
- Its comments say why: botnets guess the weak passwords on accounts that students create in class.

### Resolving a chain entry
- **File or library.** If the entry names an existing file, use the file. Otherwise, look it up in the library. The output names the source for each entry, e.g. `harden (library)` or `./setup.sh (file)`.
- **Unknown entry.** If the entry is neither a file nor a library name, stop before anything is created or changed. The error lists the library names and points to `pvelab scripts`.
- **Checks.** Every resolved script goes through the existing checks: it is at most 1 MiB, and it starts with `#!`. In `create`, all checks happen before any VM is cloned, as `--script` does today.

### Running a chain
- **Order and parallelism.** On each VM, the scripts of its chain run one after another. Different VMs run in parallel, as today.
- **Failure.**
  - If a script fails on a VM (non-zero exit, timeout, or the agent never answers), the rest of that VM's chain is skipped. Other VMs carry on.
  - The summary says, for each failed VM, which script failed.
- **Logs.** Each VM gets one log per script, under the run's log directory.
- **Timeout.** `--timeout` applies to each script.

### Snapshots
- **Name.** The name is the chain's script names joined with `-`. For example, `harden+docker` gives `harden-docker`. The name must fit PVE's rules: start with a letter, use `[A-Za-z0-9_-]`, and stay within 40 characters including the `before-` prefix. `script_snapshot_name` already enforces this.
  - A long chain is shortened in a deterministic way, so that the same chain always gives the same name.
- **`pvelab run`.**
  - Before anything starts, take `before-<name>` on every VM that will run.
  - Take `<name>` only on VMs where the whole chain succeeded.
  - An older snapshot with the same name is replaced, as today.
- **`pvelab create`.**
  - The existing `clean` snapshot, taken after first boot, is the "before" snapshot.
  - After the chain, take only `<name>`, on VMs where the whole chain succeeded.
  - VMs with different chains (`--script harden+docker,harden`) get different names: `harden-docker` on `-1` and `harden` on `-2`.
- **Messages.** The rollback hints printed today ("back to it: pvelab rollback ... --to X") stay. They refer to the pair.

### Snippet on every node
- The snippet lives in `/var/lib/vz/snippets/`, which is local to each node.
- `create` already calls `ensure_snippet()`. Keep that call, and make it rewrite the file only when the content differs.
- **Builder's call, decided:** `ensure_snippet()` also runs before every write command that runs as root, not only `create`. Otherwise a hand clone made after `pvelab upgrade` but before the next `create` would still get the old vendor-data. The cost is one file read and a compare.
- **Out of scope:** making `create` place VMs on other nodes. That comes with multi-node support.

### Warning: `--expose` without `harden` (instructor's decision)
- When `create --expose` is used and some VMs' chain does not include `harden`, pvelab warns before creating anything.
  - The warning says that accounts students make with weak passwords can be logged into over ssh from outside, and suggests `--script harden`.
  - It names the affected VMs: all of them, or the `-k` positions.
- It asks to continue, the same way the existing resource warnings do (`confirm`). `LAB_YES=1` skips the question.
- The wizard shows the same warning when "ssh from outside" is chosen and no VM has `harden`.
- `pvelab expose` on an existing net gives the same warning for VMs that never ran `harden`. Builder's call: detect this from the snapshot name or a marker file that `harden.sh` writes; if that cannot be detected reliably, warn only in `create`.

### `pvelab scripts`
```
$ pvelab scripts
  docker   Install Docker Engine + compose; student can use docker without sudo
  harden   Only "student" may ssh in; passwords need 10+ characters
use: pvelab create ... --script docker   or   pvelab run VM... harden+docker
$ pvelab scripts docker      # prints the script
```
- It is read-only, and it does not need root.
- The help for `create` and `run` mentions it.
- `create`'s help no longer says "Against botnets: only student may ssh in, passwords need 10+ characters". It now says that the `harden` script does this. The mining-port sentence stays, because those rules are still always on.

### CI
- Add a check that runs in the existing `pages.yml` build job before `build.py`. A unit test that globs `scripts/` is fine, since CI already runs pytest.
- It fails the build unless every `scripts/*.sh` meets all of the following:
  - line 1 starts with `#!`
  - line 2 matches `# <text>`, and `<text>` is non-empty, ASCII-only, and contains letters
  - every comment line (a line whose first non-blank character is `#`) is ASCII-only, so comments are in English. Strings the script prints to students may be in any language.
  - the name matches `[a-z][a-z0-9-]*`
  - the file is at most 1 MiB
  - `bash -n` passes, for files whose shebang is bash or sh

### Wizard
- **create.** The single question "Setup script to run in every VM" is replaced by a question for each of a student's VMs: "scripts for VM -k". The current library is printed above it.
  - The answer is names or paths joined with `+`. An empty answer means none.
  - Each part is validated: it must be a library name or an existing file.
  - The wizard then builds the `--script` per-VM list.
- **run.** The question becomes "Scripts (library names or files, joined with +)", and it is validated the same way.

## Tests

### Unit tests
- vendor-data has no `AllowUsers` and no pwquality, and still has `ssh_pwauth`, `qemu-guest-agent` and `package_update`. This replaces the current assertion in `test_parsing.py`.
- Chain parsing: `+`, `,`, empty entries, and per-VM expansion through `per_vm`.
- Resolution: file before library, unknown entry, and the source label shown.
- Snapshot naming for chains, including truncation.
- Chain execution: a failure stops that VM's chain, and the after-snapshot is taken only for VMs where the whole chain succeeded.
- `pvelab scripts` output.
- The CI header checks.

### Self-test (`tests/selftest.sh`)
- Uses self-test nets only.
- Create with `--script harden+<x>,` on a `Nx2` class. Check that:
  - the chain ran on `-1` and not on `-2`
  - the snapshot names are right
  - `10-pvelab.conf` exists on `-1` only
- A clone without scripts has no `10-pvelab.conf` and no pwquality file, but does have the guest agent and ssh password login.

## Left to the builder

- Module and function layout, and where chain parsing lives.
- How the library is read from the zip.
- The exact wording of the wizard and help text.
- The exact truncation scheme for snapshot names, provided it is deterministic and unique enough.
- Whether the CI check is a pytest test or a separate step.

## Amendment (2026-10-31)

`harden` no longer writes `AllowUsers student`: it would tie the repo to one user name. It only sets the password rule (pwquality: 10+ characters, no dictionary words, root included).
