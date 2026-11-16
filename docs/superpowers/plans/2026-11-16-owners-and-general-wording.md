# Owners, Tags and General Wording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** pvelab stops deriving who a VM belongs to (and whether it is a net's shared VM) from its name, keeps that in PVE instead, and speaks of owners and users, so it serves general PVE management with classrooms as one use.

**Architecture:** A new `owners.py` reads a VM's group from its PVE description tokens (`owner=KEY`, `role=shared`), falling back to today's name parsing only for VMs not yet migrated. New VMs always get the tokens and a PVE tag `net-NAME`; existing managed VMs get them once, automatically, with no reboot. With identity out of names, `net rename` keeps VM names unless asked, and the two rename paths share one core.

**Tech Stack:** Python >= 3.11, stdlib only; pytest + pyflakes; PVE 9 (`qm set --description/--tags/--name`).

**Spec:** this conversation's decisions (2026-11-16), summarized in Global Constraints. Earlier design: Notion doc https://app.notion.com/p/40be01c89557814182aee72d8da2becc (section 狀態一致性: PVE is the source of truth).

## Global Constraints

- Identity tokens in the VM description line (after `managed-by=lab.sh template=N net=N`): `owner=KEY`, and `role=shared` on a net's shared VM only. The legacy token `student=KEY` is read as `owner=KEY` and rewritten by the migration.
- KEY carries no net name; it is unique within a net. A group is the pair (net, KEY): two nets may both have owner `user01`.
- Default KEY: for created names `NET-PREFIXnn[-k]` it is `PREFIXnn`; for `--accounts` it is the list's name without a leading `NET-` (or the user); `rename` never changes it.
- Managed-VM marker tag: new VMs get tags `pvelab;net-NAME`. `is_managed` accepts `pvelab` or the legacy `lab-student` (LAB_TAG still overrides). The migration adds `pvelab` and `net-NAME`, keeps `lab-student` (other tools may look for it).
- PVE description and tags change with `qm set --description` / `--tags`: no cloud-init rerun, no reboot. Rollback keeps the current description (PVE AbstractConfig keeps `description`); tags follow the snapshot (handle after rollback in Task 5).
- Defaults: `LAB_PREFIX` (VM name prefix) default changes from `student` to `user`; new `LAB_USER` (login) default `user`. A net that already has VMs keeps its existing prefix and login when more are created.
- `create` names VMs `NET-PREFIXnn` by default; names from `--accounts` and `rename` are used as given (checked by `accounts.vm_name_problem`).
- `net rename` keeps VM names by default (no reboot); `--rename-vms` renames those starting with `OLD-` (running ones reboot).
- Wording in help, menu, README and messages: owner / user / network instead of student / class (glossary in Task 6). Classroom use is described as one example in README.
- No class downtime on workshop1 net2 (15 VMs): the migration there only edits descriptions and tags; live tests use test nets (t97...) only.
- Commits: no Co-Authored-By / Claude-Session lines. Every task ends with `~/.local/bin/uvx pyflakes src tests build.py` clean and `~/.local/bin/uvx --with pytest pytest -q tests/unit` green (also `--python 3.11`).

## Review Focus

1. A net with old VMs (no tokens) and new VMs (tokens) mixed: groups, passwords, isolation must treat an old `net2-student03` and a new `net2-student03-2` with `owner=student03` as one owner.
2. Two nets each with owner `user01`: handout cards, passwd groups and move must not merge them (group is (net, KEY)).
3. A VM renamed by hand in the PVE GUI before migration (name no longer starts with `NET-`): the migration must stop and name it, not guess.
4. Rollback after migration: the description keeps `owner=`; the tags may come back from the snapshot without `net-NAME` and must be put back.
5. `create` adding VMs to a net made with the old defaults: new VMs continue `net2-student16`, login `student`, not `net2-user16` / `user`.

---

### Task 1: owners.py — reading a VM's group

**Files:**
- Create: `src/pvelab/owners.py`
- Modify: `src/pvelab/isolation.py` (remove `student_of`, `key_of_conf`, `by_student`, `vms_per_student`, `is_shared`, `students_only`; keep `shared_name` for the default shared VM name; re-export nothing)
- Test: `tests/unit/test_owners.py`

**Interfaces:**
- Produces:
  - `owner_from_name(name: str, net_name: str | None = None) -> str` — today's `student_of` rule, then a leading `f"{net_name}-"` stripped when given.
  - `owner_of(conf: dict | None, name: str | None = None) -> str` — `owner=` token, else legacy `student=` (net prefix stripped), else `owner_from_name`.
  - `is_shared_vm(conf: dict | None, net: str | None = None) -> bool` — `role=shared` token, else (legacy) name == `isolation.shared_name(net)`.
  - `group_of(conf: dict | None, name=None) -> tuple[str, str]` — `(desc net, owner_of(...))`.
  - `by_owner(vms: dict[int, str], confs: dict[int, dict] | None = None) -> dict[str, list[int]]` — VMs of one net only; shared VMs left out.
  - `vms_per_owner(vms: dict[int, str], confs=None) -> int`.
  - `with_tokens(description: str, **tokens: str | None) -> str` — set/replace/remove (`None`) `key=value` tokens on the marker line, other text kept.

- [ ] **Step 1: Write the failing tests** `test_owner_of_reads_token_then_legacy_then_name`, `test_is_shared_vm_by_role_or_legacy_name`, `test_by_owner_mixed_old_and_new`, `test_with_tokens_sets_replaces_and_removes`:
```python
assert owners.owner_of({"description": "managed-by=lab.sh net=2 owner=user01"}, "x") == "user01"
assert owners.owner_of({"description": "managed-by=lab.sh net=2 student=net2-student03"}, "net2-db", ) == "student03"
assert owners.owner_from_name("net2-student03-2", "net2") == "student03"
assert owners.is_shared_vm({"description": "managed-by=lab.sh net=2 role=shared"}) and \
    owners.is_shared_vm({"name": "net2-shared", "description": "managed-by=lab.sh net=2"}, "2")
assert owners.by_owner({1: "net2-student03", 2: "x"}, {1: {"name": "net2-student03", "description": "net=2"},
                        2: {"name": "x", "description": "net=2 owner=student03"}}) == {"student03": [1, 2]}
assert owners.with_tokens("managed-by=lab.sh net=2 student=a\nnote", owner="b", student=None) == \
    "managed-by=lab.sh net=2 owner=b\nnote"
```
- [ ] **Step 2:** run them, expect ImportError.
- [ ] **Step 3:** implement `owners.py`; `net_name` for stripping comes from `nets.net_name(desc net)`.
- [ ] **Step 4:** switch every caller (create, handout, passwd, move, readdress, wizard, isolation's nft code) to the owners API; `by_student`→`by_owner`, `key_of_conf`→`owner_of`, `is_shared(name, n)`→`is_shared_vm(conf, n)` (callers that only have rows read the conf with `read_conf` or pass `confs`). handout and passwd group per net (call `by_owner` per net) so equal KEYs on two nets stay apart (Review Focus 2).
- [ ] **Step 5:** run all tests; fix the ones asserting old names of helpers; pyflakes clean.
- [ ] **Step 6:** commit `owners: a VM's group from owner=/role= in its description (name parsing only for VMs not migrated)`.

### Task 2: writing tokens and tags on new VMs

**Files:**
- Modify: `src/pvelab/config.py` (`TAG` default `pvelab`, `LEGACY_TAGS = ("lab-student",)`, `PREFIX` default `user`, `GUEST_USER = os.environ.get("LAB_USER", "user")`), `src/pvelab/inventory.py` (`is_managed` accepts `TAG` or `LEGACY_TAGS`), `src/pvelab/lifecycle.py` (`clone_vm(..., owner: str, shared: bool = False)` writes `owner=`, `role=shared`, tags `f"{TAG};net-{nets.net_name(net)}"`; DHCP VMs get tag `net-dhcp`), `src/pvelab/commands/create.py` (keys = owners; net's existing prefix and login reused)
- Test: `tests/unit/test_owners.py`, adjust `tests/unit/test_accounts.py`, `test_ux.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `create.net_defaults(net, inv) -> tuple[str, str]` — `(prefix, login)`: the most common `PREFIX` among the net's VMs named `NET-<letters><digits>…` and their most common login (`inventory.login_of`), else `(PREFIX, GUEST_USER)`.

- [ ] **Step 1:** failing tests `test_new_vm_gets_owner_role_and_net_tag` (fake `clone_vm` records the `qm set` args: `--tags pvelab;net-net95`, description ends `owner=user01`), `test_shared_vm_gets_role_shared`, `test_more_vms_keep_the_nets_old_prefix_and_login` (inventory with `net2-student01` login `student` → new VM `net2-student02`, user `student`), `test_is_managed_accepts_old_and_new_tag`.
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement; `--accounts` owner = list name minus `f"{net_name}-"`, or the user when the name was defaulted.
- [ ] **Step 4:** tests green, pyflakes clean.
- [ ] **Step 5:** commit `new VMs: owner=, role=shared and the net-NAME tag; defaults user/user, a net keeps its own`.

### Task 3: migration of existing VMs

**Files:**
- Create: `src/pvelab/migrate.py`
- Modify: `src/pvelab/cli.py` (call `migrate.ensure(dry)` before running a "write"/"dry" command, after `cfs_check`, like the rename resume)
- Test: `tests/unit/test_migrate.py`

**Interfaces:**
- Produces:
  - `migrate.plan(inv: dict[int, dict]) -> tuple[dict[int, tuple[str, str]], list[str]]` — `{vmid: (new description, new tags)}` for managed VMs missing `owner=`/`role=` or the `pvelab`/`net-NAME` tags, and a list of problems (VM names) it can't decide.
  - `migrate.ensure(dry: bool) -> None` — nothing to do: silent. Problems: die listing them with `pvelab owner VM KEY` as the fix. Else one line "pvelab: recording owners in N VM(s) (description and tags, no reboot)" and `qm set` each (in parallel, on its node); on a dry run only that line plus "(dry run: not done)".
  - new command `pvelab owner VM... KEY` (commands/owner.py, "write"): sets `owner=KEY` on the VMs named (the fix for problems; also to regroup by hand).
  - `pvelab owner --check` ("read"): prints `migrate.plan` (each VM: name, owner it would get, tags) and the problems; changes nothing.

- [ ] **Step 1:** failing tests `test_plan_derives_owner_from_old_names`, `test_plan_marks_the_old_shared_vm`, `test_plan_rewrites_legacy_student_token`, `test_plan_reports_a_vm_whose_name_does_not_tell` (managed VM `web01` on net 2 without token → problem; Review Focus 3), `test_ensure_is_silent_when_done`, `test_owner_command_sets_the_token`, `test_owner_check_changes_nothing`.
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement; the name rule: an old VM on net N tells its owner only if its name is `NET-<rest>` (then `owner_from_name`) or it is the net's shared name.
- [ ] **Step 4:** tests green.
- [ ] **Step 5:** commit `migrate: owner=, role=shared and tags recorded on existing VMs once (no reboot); pvelab owner VM KEY`.

### Task 4: net rename keeps VM names; one rename core

**Files:**
- Modify: `src/pvelab/readdress.py` (`rename_vms(c: Change, names: dict[int, str]) -> None`: config first, credentials after, reboot running; used by `net_rename` with `--rename-vms` and by `vm_rename`; `net_rename(old, new, dry=False, rename_vms=False)` always updates the `net-` tag of every VM of the net; drop the student=-rewrite and the token-writing in `vm_rename`, which Task 1/3 make unnecessary; allow renaming the shared VM), `src/pvelab/commands/netcmd.py` (`--rename-vms`), `src/pvelab/sdn.py` unchanged
- Test: `tests/unit/test_accounts.py` (rename tests), `tests/unit/test_nets.py`

**Interfaces:**
- Consumes: Task 1 (`owner_of`, `with_tokens`), Task 2 (tags).
- Produces: `readdress.set_net_tag(vmid: int, node: str, net_name: str) -> None`.

- [ ] **Step 1:** failing tests `test_net_rename_keeps_vm_names_by_default` (plan has no reboot step; names unchanged; tags become `net-NEW`), `test_net_rename_rename_vms_renames_old_prefix_only`, `test_rename_the_shared_vm_keeps_it_shared`, `test_rename_core_config_before_credentials` (moved from the existing failed-rename test).
- [ ] **Step 2:** run, expect failures.
- [ ] **Step 3:** implement.
- [ ] **Step 4:** tests green.
- [ ] **Step 5:** commit `net rename keeps VM names (--rename-vms to rename them); one rename core; the shared VM can be renamed`.

### Task 5: tags after a rollback

**Files:**
- Modify: `src/pvelab/commands/snapshots.py` (after `qm rollback`, put back `pvelab;net-NAME` tags if the snapshot's tags lack them: `readdress.set_net_tag`)
- Test: `tests/unit/test_owners.py`

- [ ] **Step 1:** failing test `test_rollback_puts_the_net_tag_back` (fake run records `qm set --tags` after `qm rollback` when the conf's tags miss `net-NAME`; Review Focus 4).
- [ ] **Step 2–4:** run, implement, green.
- [ ] **Step 5:** commit `rollback: the VM's net tag is put back`.

### Task 6: wording — help, menu, messages, README

**Files:**
- Modify: `src/pvelab/helptext.py`, `src/pvelab/wizard.py`, `src/pvelab/commands/*.py` messages, `src/pvelab/ui.py`, `src/pvelab/placement.py`, `src/pvelab/isolation.py`, `README.md`, tests asserting texts.
- Glossary (English UI; keep sentence style of the file):

| was | becomes |
|---|---|
| student / students | owner / owners (a VM's group); user (the login) |
| class / a class's network | network |
| VMs per student, 20x3 = 20 students x 3 VMs | VMs per owner, 20x3 = 20 owners x 3 VMs |
| students kept apart / reach each other | owners kept apart / reach each other |
| Create VMs for a class | Create VMs |
| login cards for students | login cards |
| shared VM: every student reaches it | shared VM: every owner reaches it |

- [ ] **Step 1:** `grep -rniE "student|class" src/pvelab README.md` and change user-facing strings per the glossary; identifiers may keep old names only where renaming them is out of this plan's scope (list any left in the commit message).
- [ ] **Step 2:** README: a general introduction (managing VMs on PVE: networks, owners, accounts, isolation), then "Example: a classroom" with the former class wording.
- [ ] **Step 3:** run tests; update text assertions; pyflakes clean.
- [ ] **Step 4:** commit `wording: owners, users and networks; a classroom is one example`.

### Task 7: live test and rollout

- [ ] **Step 1:** build and install to `~/.local/bin/pvelab`.
- [ ] **Step 2:** on a test net t97: create 2x2 (tokens, tags), `owner`, rename, `net rename` with and without `--rename-vms`, rollback (tag back), passwd, handout, destroy.
- [ ] **Step 3:** on workshop1 run `pvelab owner --check` (read-only) and show its plan for net2's 15 VMs to the user before any write command runs there.
- [ ] **Step 4:** ask the user; then run the migration on net2 (descriptions and tags only).
