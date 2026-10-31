# pvelab script library: implementation plan

Spec: `docs/superpowers/specs/2026-10-31-script-library-design.md`. If this plan and the spec disagree, the spec wins.
The plan has three chunks: A, then B, then C. After each chunk, all of these must pass:

```
cd /home/seanyeh77/pvelab-src
PYTHONPATH=src timeout 120 uvx -q --with pytest pytest -q tests/unit
timeout 60 uvx -q pyflakes src tests build.py
python3 build.py
```

Rules:
- Python 3.11.
- Short functions, with comments in plain English.
- No git commits.
- Never run `tests/selftest.sh` or pvelab against the host.

---

## Chunk A: clean vendor-data, the library, `pvelab scripts`, CI check

### A1. Vendor-data with only the three relaxations
- **`src/pvelab/templates.py`**
  - `vendor_data()` returns only:
    - `#cloud-config`
    - `ssh_pwauth: true`
    - `package_update: true`
    - `packages: [qemu-guest-agent]`
    - a `runcmd` that starts qemu-guest-agent

    Drop the `write_files` block (`10-pvelab.conf` and pwquality), `libpam-pwquality`, and the ssh reload.
  - Rewrite the docstring: templates restrict nothing, and `harden` is now a library script.
  - Remove the imports of `PW_MINLEN` and `GUEST_USER` if nothing else uses them. Keep `PW_MINLEN` in config only if something still reads it.
- **`ensure_snippet()`**
  - Keep `write_file()`, which already skips writing when the content is the same.
  - Run `pvesm set` only when `snippets` is missing, as today.
  - Return what `write_file` returns.
  - Move the paths into `config.py` as `STORAGE_CFG = "/etc/pve/storage.cfg"` and `SNIPPET_DIR = "/var/lib/vz/snippets"`, so tests can point them at `tmp_path`.
- **`src/pvelab/cli.py`**
  - In `main()`, right after the lock is taken (write commands only, not read or `--dry-run`), call `ensure_snippet()` and add the comment: "hand clones of a template get the current vendor-data".
  - Keep the call in `create`.

### A2. `scripts/` folder
- **`scripts/docker.sh`**: copy `/home/seanyeh77/docker.sh`.
  - Replace its 2-line header comment with exactly: `# Install Docker Engine + compose; student can use docker without sudo`
  - Move the old "Safe to run again" text to line 3.
- **`scripts/harden.sh`**: `#!/bin/bash`, then the line `# Only "student" may ssh in; passwords need 10+ characters`, then:
  - `set -euo pipefail`
  - `DEBIAN_FRONTEND=noninteractive`
  - A comment explaining why: botnets guess the weak passwords on accounts students make in class (`adduser bob`). Those accounts still work locally (`su - bob`).
  - Write `/etc/ssh/sshd_config.d/10-pvelab.conf` with `AllowUsers student`.
  - `apt-get install -y -q libpam-pwquality`
  - Write `/etc/security/pwquality.conf.d/pvelab.conf` with `minlen = 10` and `enforce_for_root`.
  - `systemctl reload ssh || systemctl reload sshd`
  - Use plain overwrites only (`printf > file`, `mkdir -p`), so running it twice is safe.

### A3. Packing (`build.py`)
- `SCRIPTS = ROOT / "scripts"`
- `libs = sorted(SCRIPTS.glob("*.sh"))`
- Add each file as `pvelab/library/<name>.sh`.
- `version()` hashes `p.relative_to(ROOT)` + bytes for `files + libs`. Changing a script alone then changes the version.
- The "built ..." line also prints the script count.

### A4. Library loader: new `src/pvelab/library.py`
- `NAME_RE = re.compile(r"[a-z][a-z0-9-]*")`
- `_dir()`
  - Returns `importlib.resources.files("pvelab") / "library"` when that `.is_dir()` (inside the zip app).
  - Otherwise it returns `Path(__file__).resolve().parents[2] / "scripts"` (from source, or in tests).
  - Otherwise `None`.
- `library()` returns `{name: (description, data_bytes)}`, sorted by name, from every `*.sh` file there. The description is line 2 with `# ` removed.
- `library_lines()` returns the aligned `name  description` rows. `pvelab scripts` and the wizard both use them.

### A5. `pvelab scripts [NAME]`: new `src/pvelab/commands/scripts.py`
- **`cmd_scripts(args)`**
  - With no args: print `library_lines()`, then the `use:` line from the spec.
  - With `NAME`: write the script's bytes to stdout.
  - Unknown name: `die`, listing the names and adding `did_you_mean`.
  - More than one arg: `bad_usage`.
- **`cli.py`**
  - Handle `scripts` before `pve_check()`, as `upgrade` is handled: no root, no lock.
  - Add it to `NO_ROOT`.

### A6. Tests (Chunk A)
- **`tests/unit/test_parsing.py`**: replace `test_vendor_data_hardens_ssh_and_passwords` with `test_vendor_data_only_relaxes`. It checks:
  - `ssh_pwauth: true`, `qemu-guest-agent` and `package_update: true` are present.
  - `AllowUsers` and `pwquality` are absent.
- **New `tests/unit/test_library.py`**
  - `test_ensure_snippet_writes_only_on_change`
    - Monkeypatch `STORAGE_CFG` and `SNIPPET_DIR` to `tmp_path`, and record calls to `run`.
    - The first call returns True. The second returns False, and the file mtime is unchanged.
    - `pvesm` is not called when `content` already lists `snippets`.
  - `test_library_from_source`: `docker` and `harden` are listed with their descriptions.
  - `test_cmd_scripts_list_and_print` (capsys) checks:
    - the list output
    - the `use:` line
    - that `scripts harden` prints the file
    - that an unknown name raises `LabError` and names `harden`
  - `test_script_headers` is the CI check. It loops over `ROOT/"scripts"/*.sh` and requires:
    - line 1 starts with `#!`
    - line 2 matches `# (.+)`, and the text is ASCII and has letters
    - every comment line (first non-blank char `#`) is ASCII
    - the name matches `NAME_RE`
    - the file is at most 1 MiB
    - `bash -n` passes when the shebang ends in `bash` or `sh`

    It also asserts that the folder is not empty.
- **`tests/unit/test_build.py`**
  - `test_build_ships_library`: `dist/pvelab scripts` lists `harden`, and `dist/pvelab scripts docker` begins with `#!`. This exercises the zip loader.
  - `test_script_change_changes_version`: monkeypatch `build.version` with a temporary extra file, or call `version()` on two file lists with different script bytes and check the hashes differ.
- CI: no change to `.github/workflows/pages.yml`. `pytest` already runs before `build.py` and now includes `test_script_headers`.

---

## Chunk B: chains, resolution, execution, snapshots, create and run, expose warning

### B1. Parsing and resolution (`src/pvelab/scripts.py`)
- **`Script`** changes to the namedtuple `Script(label, base, data, name, source)`.
  - `name`: the snapshot stem. Library: the name. File: `snap_stem(path)`.
  - `source`: `"library"` or `"file"`.
  - `label`: e.g. `harden (library)` or `./setup.sh (file)`.
- **`snap_stem(path)`**: today's sanitising from `script_snapshot_name`, without the length and reserved checks:
  - strip the extension
  - replace characters outside `[A-Za-z0-9_-]` with `-`
  - add `s-` in front when the stem does not start with a letter
- **`resolve_entry(entry)`**
  - If `os.path.isfile(expanduser(entry))`, it is a file: `check_script` (1 MiB, `#!`).
  - Else, if the name is in `library()`, it is a library script. Run the same size and `#!` checks on its bytes.
  - Else `die`: `unknown script 'x': not a file, not in the library (docker, harden) - see: pvelab scripts`.
- **`parse_chain(text)`**
  - `""` gives `()`.
  - If the whole text is an existing file, it is one entry (a path that contains `+` keeps working).
  - Otherwise split on `+` and strip the parts. An empty part (`a++b`, `+a`) is a `bad_usage`.
  - Returns a tuple of `resolve_entry` results, and checks that the chain's snapshot name is valid (B3).
- **`print_chain(chain, where)`** prints `>> scripts for VM -1: harden (library) + ./x.sh (file)`, so the source is always visible.

### B2. Per-VM chains
- **`create.py`**: `script` becomes `chains = per_vm("--script", script or "", per, parse_chain)`.
  - `per_vm` already splits on `,`, takes one value for all VMs or one for each, and fails on any other count.
  - `harden+docker,` gives `[(harden, docker), ()]`.
  - All parsing and checks run before `free_net()`, `ensure_bridge` or any clone, as today.
- **`run.py`**
  - The last positional is the chain: `chain = parse_chain(rest[-1])`.
  - An empty chain is a `bad_usage`.
  - `chains = {vmid: chain for vmid in targets(vms)}`

### B3. Snapshot names (`scripts.py`)
- **`chain_snapshot_name(chain)`**
  - `n = "-".join(s.name for s in chain)`
  - If `len(PRE + n) > 40`: `n = n[:33 - 7].rstrip("-") + "-" + sha1(n)[:6]`. This is deterministic, and different long chains get different names.
  - Then the existing checks: at least 2 characters, not `current`, `clean` or `initial`, no `before-` prefix. Otherwise `die`.
- **`script_snapshot_name(path)`** stays as `chain_snapshot_name` for one file, so the old tests keep their meaning. The long-name test still holds: the result fits within 40 characters, `before-` included.

### B4. Running chains: `run_script` and `run_and_snapshot` are replaced
- **`run_chains(chains, timeout_min, pre_snap)`**, where `chains` is `{vmid: tuple[Script]}` and the empty chains are already removed.
  1. Running check, agent wait and the `ready` list, as today: run once, before step 1.
  2. If `pre_snap`, take `before-<chain name>` on every ready VM before anything starts. `Progress.each` is grouped by snapshot name.
  3. Make the log dir `RUN_LOG_DIR/<time>-<name>`. `<name>` is the chain name when all chains are the same, else `scripts`.
  4. Step `k = 0, 1, ...`: `start_and_wait({vmid: chain[k]}, names, timeout_min)` runs on the VMs that are still OK and have a k-th script. This is today's start, poll and progress loop, taking a per-VM `Script` with `--timeout` applied to each step. VMs within a step run in parallel. A VM whose script fails (exit ≠ 0, timeout, or no agent) leaves the set, so the rest of its chain is skipped.
  5. Each script's log goes to `<logdir>/<vm>/<k+1>-<name>.log`, with the same header and stderr section as today.
  6. Returns `{vmid: None}` when the whole chain succeeded, or `(script, exitcode)` for the step that failed. VMs that were never run get `(chain[0], None)`.
- **`run_and_snapshot(chains, timeout_min, pre_snap)`**
  - Calls `run_chains`.
  - Takes `<chain name>` only on VMs where the whole chain succeeded, grouped by name.
  - Keeps today's hints: `back to it: pvelab rollback <VM> --to X`, `to undo ...: --to before-X`, and `run it again there with: pvelab run <VM> <chain>`.
  - Summary line: `failed: net40-student01-1 (docker, exit 1) net40-student02-1 (harden, exit -)`.
  - Returns a failure dict that `run.py` uses for `sys.exit(1)`.
- **`create.py`**
  - `made` stores `(vmid, k)`.
  - After `clean`: `run_and_snapshot({i: chains[k] for i, k in made if i in done and chains[k]}, ...)` with `pre_snap=False`, because `clean` is the "before" snapshot.

### B5. Warning: `--expose` without `harden`
- **`scripts.py`**: `expose_warning(chains, per)` returns the warning lines, or `[]` when every chain contains the library `harden` (`source == "library" and name == "harden"`).
  - It names the affected VMs: `all VMs`, or `VM -1, -3`.
  - The text: students' accounts with weak passwords could be logged into over ssh from outside; add `--script harden`.
- **`resources.confirm(problems, skip=FORCE, why="LAB_FORCE=1")`** gets a keyword so that `LAB_YES=1` skips the question here: `confirm(lines, skip=YES, why="LAB_YES=1")`. `create` calls it when `expose` is set, before `free_net()`.
- **`pvelab expose`: no warning (builder's call, per the spec).** `harden` can't be detected reliably on an existing net:
  - Snapshot names get truncated or hashed, and can be rolled back past.
  - `harden` may have been run from a file.
  - A marker file needs the guest agent on running VMs.

  So the warning is only in `create`. Write this decision as a comment in `commands/access.py`.

### B6. Tests (Chunk B): new `tests/unit/test_chains.py`
- `test_parse_chain`:
  - `harden+docker` gives 2 library scripts with labels.
  - `""` gives `()`.
  - `a++b` raises `LabError`.
- `test_per_vm_chains`: `per_vm("--script", "harden+docker,", 2, parse_chain)` gives `[(h, d), ()]`. With 3 values for `per=2`, it raises an error.
- `test_file_wins_over_library`: `tmp_path/"harden"` containing `#!/bin/sh` resolves to `source == "file"` after `chdir`. Also check that an unknown entry's error lists `docker, harden` and `pvelab scripts`, and that a file without `#!` is refused.
- `test_chain_snapshot_name`:
  - `harden+docker` gives `harden-docker`.
  - A long chain fits within 40 characters with `before-`, the same chain gives the same name, and two long chains give different names.
  - Reserved names are refused.
- `test_chain_stops_on_failure`
  - Monkeypatch `vm_statuses`, `agent_up`, `guest_json` (the exec gives exited with a scripted exit code per (vm, script)), `take_snapshot` (records the calls), `vm_names` and `RUN_LOG_DIR` (`tmp_path`).
  - VM 1 `a+b` all OK, VM 2 `a+b` fails at `a`. Check that:
    - `b` never ran on VM 2
    - the after-snapshot `a-b` exists on VM 1 only
    - `before-a-b` exists on both (with `pre_snap`)
    - the logs `vm1/1-a.log` and `vm1/2-b.log` exist
- `test_expose_warning`: all chains with `harden` give `[]`. `harden,` with `per=2` names `-2`. No scripts names `all VMs`.
- **`tests/unit/test_ux.py`**: update `test_create_per_vm_specs` to pass `--script harden,` and check that nothing runs, because `wait_first_boot` returns `{}`. Add a `create` case where `--expose` without `harden` and `LAB_YES` unset off a tty dies before `clone_vm`.

---

## Chunk C: wizard, completion, help, README, selftest

### C1. Wizard (`src/pvelab/wizard.py`)
- **`check_chain(a)`**: for an answer check, it returns `None` or the first line of the error, by wrapping `parse_chain` with `valid()`. An empty answer is allowed.
- **`show_library()`** prints `library_lines()` dimmed, plus `or a file path; join several with +`.
- **`create()`**: the old "Setup script" question is replaced.
  - Call `show_library()`.
  - Ask `per` questions: `Scripts for VM -{k} (empty: none)`. Use `Scripts for every VM` when `per == 1`.
  - Then `--script joined(answers)`, only if any answer is non-empty. `joined` already gives `harden+docker,` when the answers differ.
  - Then, if `--expose` is chosen and an answer lacks `harden`, print the warning in yellow, from `expose_warning`. Create itself asks again when it runs.
- **`run()`**: `show_library()`, then `Scripts (library names or files, joined with +)` with `check=check_chain`, required.
- **`MENU`**: the run label becomes `Run scripts in VMs`.

### C2. Completion (`src/pvelab/commands/complete.py`)
- Add `"scripts"` to `COMMANDS`, and `OPTIONS["scripts"] = []`.
- `chain_candidates(cur)`: keep everything up to the last `+`, then add each library name that starts with the rest. `harden+d` gives `harden+docker`.
- In `candidates()`:
  - `prev == "--script"` gives `chain_candidates(cur)`. When nothing matches, bash falls back to file names.
  - `run` adds `chain_candidates(cur)` to its VM candidates.
  - `scripts` at `cword == 2` gives library names.
- Tests (`tests/unit/test_ux.py`):
  - `--script h` → `harden`
  - `--script harden+d` → `harden+docker`
  - `run net40-student01 do` → includes `docker`
  - `scripts ` → `docker`, `harden`

### C3. Help (`src/pvelab/helptext.py`)
- **USAGE**
  - Change the `create` line to `[--script SCRIPTS]`.
  - Change `run` to `run VM... SCRIPT[+SCRIPT...] [--timeout MIN]`.
  - Add `scripts [NAME]   list the script library, or print one`.
- **`create` help**
  - `--script`: library names or files. `+` runs several in order. A comma gives one chain per VM of a student (`harden+docker,` = nothing on `-2`). Snapshot `<names joined by ->` after.
  - Replace the botnet sentence with: "Templates restrict nothing; `--script harden` lets only "student" ssh in and needs 10+ character passwords (recommended with --expose). Mining-pool ports are always blocked."
  - Mention `pvelab scripts`.
- **`run` help**: the same chain wording, `--timeout` per script, the snapshot pair per run, and the example `pvelab run --net 40 harden+docker`.
- **New `scripts` entry** in `command_help`.
- Tests: `help create` has no `only "student" may ssh in` sentence and mentions `harden` and `pvelab scripts`. `help scripts` exists.

### C4. README.md
- The Hardening bullet: mining ports are always blocked. `harden` (`--script harden`) restricts ssh to `student` and sets 10+ character passwords. Templates restrict nothing.
- Add a short "Script library" section:
  - `pvelab scripts`
  - `--script harden+docker,`
  - `pvelab run VM... harden+docker`
  - scripts arrive with `pvelab upgrade`
  - adding a script: the `scripts/NAME.sh` header rule
- Update the examples to `--script harden+setup.sh`.

### C5. `tests/selftest.sh` (edit only, do NOT run)
- Add `$SD/mark.sh`: `#!/bin/bash`, `touch /root/mark-ran`.
- Change the N3 create to `$L create 2x2 --net $N3 --cores 1 --memory 1024 --script harden+$SD/mark.sh,`.
- New checks in the `== scripts` section:
  - chain ran on `$C11`: `/root/mark-ran`
  - not on `$C12`
  - snapshot `harden-mark` on `$C11`, and none on `$C12`
  - no `before-harden-mark`, because create uses `clean`
  - `/etc/ssh/sshd_config.d/10-pvelab.conf` on `$C11` only
  - `$C12` (a clone with no scripts):
    - no `10-pvelab.conf`
    - no `/etc/security/pwquality.conf.d/pvelab.conf`
    - `systemctl is-active qemu-guest-agent`
    - `sshd -T | grep -qi '^passwordauthentication yes'`
  - `$L scripts | grep -q harden`, and `$L scripts harden | head -1 | grep -q '^#!'`
  - `nocheck "unknown script rejected" $L create 1 --net 93 --script no-such-script`
  - the create output names `harden (library)`: capture the N3 create output to a file and grep it
- Fix the log paths to the new layout: `run-logs/*-course-setup/$A1/1-course-setup.log` and `run-logs/*-fail-step/$B1/1-fail-step.log`.
- Finish with `bash -n tests/selftest.sh`.
