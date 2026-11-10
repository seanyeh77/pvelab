# Custom Net Name and CIDR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `pvelab create … --net NAME --cidr 192.168.50.0/24` makes a class net with its own name and address range; every other command accepts that name wherever it takes `--net N`, and nets made before keep working unchanged.

**Architecture:** The net number N stays the internal key (interfaces block markers, SDN vnet `labN`, VXLAN tag, gateway MAC, credentials.tsv, VM description `net=N`). A new module `src/pvelab/nets.py` gives each net a name and a CIDR. Both live in the net's own block in `/etc/network/interfaces`: the name as a comment line `# pvelab name NAME`, the CIDR as the stanza's `address GW/PREFIX` line. Nets without the name line are named `netN`, and a missing address means `10.10.N.0/24`. Every place that now builds `10.10.N`, `.0/24`, `.1` or `net{N}` asks `nets` instead.

**Tech Stack:** Python 3.11+ standard library (`ipaddress`, `re`), pytest, pyflakes. Run tests with `~/.local/bin/uvx --with pytest pytest -q tests/unit` and lint with `~/.local/bin/uvx pyflakes src tests` (no pytest on the system Python).

**Spec:** this conversation's decision: the user wants custom net names and IP CIDRs, CLI first (web later). The table below pins the behaviour.

| Item | Rule |
|---|---|
| Net name | `^[a-z][a-z0-9-]{0,29}$`, not ending in `-`; not `dhcp`, `auto`, `all`; not `net<digits>` unless it is `net{N}` of the same N; unique among nets |
| Default name | `net{N}` (as today) |
| CIDR | IPv4; inside 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16; prefix /16 to /27; no host bits set |
| Default CIDR | `10.10.{N}.0/24` (as today) |
| Gateway | first address (network + 1) |
| VM addresses | from network + 11 (`IP_FIRST`) up to broadcast − 1, in order; /27 gives 20 |
| Must not overlap | vmbr1 `10.10.10.0/24`, any other pvelab net, any SDN subnet in `/etc/pve/sdn/subnets.cfg`, any non-default IPv4 route on any online node |
| VM names | `{name}-student01`, `{name}-student01-2` (today `net30-student01`) |
| Number N for a new named net | `--net NAME` alone: the lowest free N (as `--net auto`); `--net N --name NAME` is not offered |
| `--cidr` | only when the net is new; on an existing net with another CIDR: refuse |
| Rename / re-address an existing net | out of scope |

## Global Constraints

- Nets created before this change (block without name line, `address 10.10.N.1/24`) behave exactly as today: same names, IPs, rules, output text except where a task says otherwise.
- `--net 30` and `--net net30` keep working everywhere; a name works everywhere a number works.
- CLI text is English, as in the rest of pvelab; help in `src/pvelab/helptext.py`.
- vmbr1 (`dhcp`, `10.10.10.0/24`) is not customisable.
- No Claude attribution in commit messages (no Co-Authored-By, no Claude-Session).
- Before Task 1: the working tree has uncommitted work (`src/pvelab/downs.py`, list/status/power changes, `tests/unit/conftest.py`). Commit it first on its own, or ask the owner; do not mix it into this plan's commits.
- Live tests only on a self-test net (Task 7); never touch net30 or net40.

## Review Focus

1. A CIDR that overlaps the hosts' LAN (10.0.0.0/24) or a route that exists only on workshop2: refuse before anything is made, naming the node and route. Test in Task 3.
2. An existing net from before (no name line, /24): name `net30`, CIDR `10.10.30.0/24`, `net30-student16` as the next VM name, same iptables/nft text as today. Test in Task 1 and Task 4.
3. A /23 net: an address like `192.168.51.0` is a valid VM address, and must not be treated as a network address. Gateway `192.168.50.1/23` in the stanza, cloud-init `ip=…/23,gw=192.168.50.1`. Test in Task 2.
4. A name that looks like another net (`net31` for a new N=32, `40`, `dhcp`) or clashes with an existing net: refuse with the reason. Test in Task 1.
5. `--cidr` with host bits (`192.168.50.7/24`): refuse and suggest `192.168.50.0/24`. `--cidr` given for a net that already exists with another CIDR: refuse, not silently ignored. Test in Task 1 and Task 5.

6. Someone has SDN changes in the PVE web UI that are not applied yet when `pvelab create`/`destroy`/`node add` runs: pvelab must stop and list them, never apply them. Test in Task 0.
7. An SDN subnet or route added by someone after a pvelab net was made, overlapping it: `pvelab list` warns in red. Test in Task 3.

---

### Task 0: never apply other people's pending SDN changes

Today `sdn.apply()` runs `pvesh set /cluster/sdn`, which applies every pending SDN change in the cluster. That includes a vnet someone is still editing in the web UI. PVE 9.2 has a global SDN lock (`pvesh create /cluster/sdn/lock` → token; `--lock-token` on zone/vnet/subnet create/delete and on `pvesh set /cluster/sdn`, with `--release-lock 1`; `pvesh delete /cluster/sdn/lock --lock-token T`). It also has pending listings (`--pending 1` on `/cluster/sdn/zones`, `/cluster/sdn/vnets`, `/cluster/sdn/vnets/{vnet}/subnets`, `/cluster/sdn/controllers`); a pending item carries `state`: `new`, `changed` or `deleted`.

**Files:**
- Modify: `src/pvelab/sdn.py` (`ensure_zone`, `add_vnet`, `remove_vnet`, `apply`), `src/pvelab/networks.py:165-180` (`make_sdn_net`), `src/pvelab/networks.py:210-225` (`remove_bridge`), `src/pvelab/commands/destroy.py:60`, `src/pvelab/joining.py:567-568`
- Test: `tests/unit/test_sdn_pending.py`

**Interfaces:**
- Produces:
  - `sdn.pending(listings: dict[str, list[dict]]) -> list[str]`: pure. `listings` maps `"zones"`, `"vnets"`, `"subnets"` and `"controllers"` to pvesh's `--pending 1` rows. Returns one line per item that has a `state`, `"vnet web1 (new)"`, leaving out pvelab's own items (zone `SDN_ZONE`; vnets `lab<digits>` in zone `SDN_ZONE`; their subnets).
  - `sdn.foreign_pending() -> list[str]`: reads the four listings (each vnet's subnets in parallel) and returns `pending(...)`.
  - `sdn.session()`: a context manager. It dies when `foreign_pending()` is not empty, with `f"SDN has changes that are not applied yet: {', '.join(items)}. Apply or roll them back (Datacenter → SDN), then run pvelab again."`. Otherwise it takes the lock with `--allow-pending 1` (pvelab's own leftovers may be pending), yields the token, and releases the lock if the body raises. Inside it, `ensure_zone`, `add_vnet`, `remove_vnet` and `apply` pass `--lock-token`, and `apply` adds `--release-lock 1`.
- Consumes: `resources.pvesh_json`, `util.parallel`, `util.run`.

- [ ] **Step 1: Write the failing tests.**
  - `test_pending_lists_only_foreign_items`: rows include zone `pvelab` (changed), vnet `lab33` in zone `pvelab` (new), vnet `web1` in zone `office` (new), a subnet of `web1` (new), and controller `evpn1` without `state`. Result: `["vnet web1 (new)", "subnet office-192.168.9.0-24 (new)"]`.
  - `test_session_refuses_with_the_list`: with `foreign_pending` faked to return `["vnet web1 (new)"]`, entering `sdn.session()` dies with `vnet web1 (new)` and `Apply or roll them back`, and `run` is never called with `/cluster/sdn/lock`.
  - `test_session_passes_the_token_and_releases_on_error`: with `run` recording calls, the lock returns `"tok1"`, and the body calls `sdn.add_vnet("33")` then raises `LabError`. Every recorded SDN create carries `--lock-token tok1`, and the last call is `pvesh delete /cluster/sdn/lock --lock-token tok1`.
  - `test_apply_releases_the_lock`: inside a session, `apply()` calls `pvesh set /cluster/sdn --lock-token tok1 --release-lock 1`.
- [ ] **Step 2: Run** `~/.local/bin/uvx --with pytest pytest -q tests/unit/test_sdn_pending.py`. Expected: FAIL (`sdn.pending` is not defined).
- [ ] **Step 3: Implement.**
  - Keep the token in a module variable set by `session()`, so the existing call sites only need wrapping, not new parameters.
  - `make_sdn_net`, `destroy`'s removal plus apply, and `joining`'s `ensure_zone` plus apply each run inside one `with sdn.session():`.
  - The undo path in `make_sdn_net` stays inside the same session.
  - `pvelab create` checks `foreign_pending()` while planning (before the plan is shown), so the refusal comes before anything is cloned.
- [ ] **Step 4: Run the whole suite.** Expected: PASS.
- [ ] **Step 5: Commit** `"sdn: never apply someone else's pending SDN changes; hold PVE's SDN lock while pvelab changes SDN"`

### Task 1: `nets` module: name and CIDR of a net

**Files:**
- Create: `src/pvelab/nets.py`
- Test: `tests/unit/test_nets.py`

**Interfaces:**
- Produces (all `n` are net numbers as strings, `"dhcp"` allowed where noted):
  - `default_cidr(n: str) -> ipaddress.IPv4Network`: `10.10.{n}.0/24`; for `"dhcp"`: `10.10.10.0/24`
  - `register(n: str, name: str, cidr: IPv4Network) -> None`: a net this run is about to make; read before the block exists
  - `cidr(n: str) -> IPv4Network`: registered, else from the block's `address` line, else `default_cidr(n)`
  - `gateway(n: str) -> IPv4Address`: `cidr(n).network_address + 1`
  - `net_name(n: str) -> str`: registered, else the block's name line, else `f"net{n}"`; `"dhcp"` → `"vmbr1(dhcp)"` (what `net_label` returns today)
  - `vm_addresses(n: str) -> Iterator[IPv4Address]`: network + `IP_FIRST` up to broadcast − 1
  - `NAME_LINE = "        # pvelab name {}\n"` (written into the block by Task 2)
  - `all_nets() -> dict[str, tuple[str, IPv4Network]]`: every pvelab net in `/etc/network/interfaces`, `{n: (name, cidr)}`
  - `resolve(v: str) -> str | None`: `"30"`/`"net30"` → `"30"` (validated with `args.valid_net`); a known name → its N; an unknown name → `None`
  - `name_problem(name: str, n: str) -> str | None`
  - `parse_cidr(v: str) -> IPv4Network` (dies with the reason: syntax, not private, prefix outside /16–/27, host bits with the suggested network)
  - `overlap(net: IPv4Network, n: str, others: dict[str, IPv4Network]) -> str | None`: pure; `others` are named ranges (`"net40"`, `"vmbr1"`, `"SDN subnet …"`, `"route on workshop2"`)
- Consumes: `config.IFACES`, `config.IP_FIRST`, `config.read_text`, `ifaces.block_begin/block_end`, `args.valid_net`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_nets.py`. Write a fake interfaces file through `monkeypatch.setattr(config, "IFACES", path)` (conftest already points it at an empty file).

```python
OLD = ("# lab.sh net 30 begin (managed by lab - removed automatically with its last VM)\n"
       "auto vmbr30\niface vmbr30 inet static\n        address 10.10.30.1/24\n# lab.sh net 30 end\n")
NEW = ("# lab.sh net 31 begin (managed by lab - removed automatically with its last VM)\n"
       "        # pvelab name docker-lab\niface lab31 inet static\n        address 192.168.50.1/23\n# lab.sh net 31 end\n")

def test_old_net_keeps_its_name_and_range():           # nets made before this change
    assert nets.net_name("30") == "net30" and str(nets.cidr("30")) == "10.10.30.0/24"
    assert str(nets.gateway("30")) == "10.10.30.1"

def test_named_net_from_its_block():
    assert nets.net_name("31") == "docker-lab" and str(nets.cidr("31")) == "192.168.50.0/23"
    assert nets.resolve("docker-lab") == "31" and nets.resolve("net30") == "30" and nets.resolve("nope") is None

def test_vm_addresses_cross_the_third_byte_in_a_23():
    a = list(map(str, nets.vm_addresses("31")))
    assert a[0] == "192.168.50.11" and "192.168.50.255" in a and "192.168.51.0" in a and a[-1] == "192.168.51.254"

def test_registered_net_wins_before_its_block_exists():
    nets.register("32", "web", ipaddress.ip_network("172.20.0.0/27"))
    assert nets.net_name("32") == "web" and len(list(nets.vm_addresses("32"))) == 20

@pytest.mark.parametrize("name,n,why", [("net31", "32", "looks like"), ("40", "32", "start with a letter"),
    ("dhcp", "32", "reserved"), ("docker-lab", "32", "already"), ("Bad", "32", "lowercase"), ("a-", "32", "end"),
    ("net32", "32", None), ("web-2", "32", None)])
def test_name_rules(name, n, why): ...       # None: accepted; else `why` is in the message

@pytest.mark.parametrize("v,why", [("192.168.50.7/24", "192.168.50.0/24"), ("8.8.8.0/24", "private"),
    ("10.0.0.0/8", "/16"), ("10.1.0.0/28", "/27"), ("nonsense", "CIDR")])
def test_bad_cidr_dies_with_the_reason(v, why): ...

def test_overlap_names_the_range():
    others = {"vmbr1": ipaddress.ip_network("10.10.10.0/24"), "route on workshop2": ipaddress.ip_network("10.0.0.0/24")}
    assert "route on workshop2" in nets.overlap(ipaddress.ip_network("10.0.0.0/23"), "32", others)
    assert nets.overlap(ipaddress.ip_network("192.168.60.0/24"), "32", others) is None
```

- [ ] **Step 2: Run** `~/.local/bin/uvx --with pytest pytest -q tests/unit/test_nets.py`. Expected: FAIL, `ModuleNotFoundError: pvelab.nets`.
- [ ] **Step 3: Implement `src/pvelab/nets.py`** with the interfaces above. Read blocks with one regex over `read_text(IFACES)` per call; the name line is `^\s*# pvelab name (\S+)$` inside the block, the address is `^\s*address (\S+)$` parsed with `ipaddress.ip_interface(...).network`. Error messages for `name_problem`: "a net name starts with a letter", "lowercase letters, digits and -", "cannot end in -", "'dhcp' is reserved", "net31 looks like another net's number", "net N is already called docker-lab".
- [ ] **Step 4: Run the test file**, then the whole suite. Expected: PASS, 0 failures.
- [ ] **Step 5: Commit** `git add src/pvelab/nets.py tests/unit/test_nets.py && git commit -m "nets: a net's name and CIDR, from its interfaces block (old nets: netN, 10.10.N.0/24)"`

### Task 2: build bridges, SDN vnets, isolation and VM IPs from the CIDR

**Files:**
- Modify: `src/pvelab/networks.py` (`bridge_stanza`, `check_free`, `foreign_use`, `ensure_bridge`, `make_sdn_net`, `ensure_gw_mac`, `next_ip`, `ensure_dhcp_bridge`), `src/pvelab/lifecycle.py:23`, `src/pvelab/sdn.py` (`subnet_id`, `conflict`, `add_vnet`), `src/pvelab/isolation.py:69-80`, `src/pvelab/commands/create.py:88,143,150,237` (same text, built from `cidr`/`gateway`: `10.10.40.0/24` and `10.10.40.1` as before), `src/pvelab/config.py` (remove `subnet()`; keep `DHCP_NET` for dnsmasq only)
- Test: `tests/unit/test_nets.py` (new tests), existing tests in `tests/unit/test_isolation_hosts.py` and `test_ux.py` must still pass unchanged

**Interfaces:**
- Consumes: Task 1 (`cidr`, `gateway`, `net_name`, `vm_addresses`, `NAME_LINE`).
- Produces: `networks.bridge_stanza(n: str, br: str, rules: list[str], sdn_made=False) -> str` (takes n, no longer `sub`); `isolation.gw_mac(n: str)` unchanged in value, but `bridge_stanza` must pass `n`, not `sub.split('.')[-1]`; `sdn.subnet_id(n) -> f"{SDN_ZONE}-{network}-{prefix}"` (for `10.10.30.0/24`: `pvelab-10.10.30.0-24`, same as today).

- [ ] **Step 1: Write the failing tests**
  - `test_stanza_of_a_23_net`: after `nets.register("31", "docker-lab", ip_network("192.168.50.0/23"))`, `bridge_stanza("31", "vmbr31", [])` contains `NAME_LINE.format("docker-lab")`, `address 192.168.50.1/23` and `MASQUERADE` with `-s 192.168.50.0/23`.
  - `test_old_net_stanza_is_unchanged`: for net 30 without registering, the stanza equals today's text byte for byte, with no name line. Before changing the code, capture today's output as a literal in the test.
  - `test_next_ip_skips_used_and_goes_past_255`: rows using `.11`–`.254` of `192.168.50.0/23` → `192.168.50.255`.
  - `test_cloud_init_ip_line`: the `ipcfg` from `lifecycle` for net 31 is `ip=192.168.50.11/23,gw=192.168.50.1`. If needed, factor it into `lifecycle.ip_config(net, ip) -> str` so the test can call it.
  - `test_sdn_vnet_args`: with `run` faked, `sdn.add_vnet("31")` calls `--subnet 192.168.50.0/23 --gateway 192.168.50.1` and `--alias "pvelab docker-lab"`.
  - `test_isolation_arp_rule_uses_the_gateway`: the nft text for net 31 has `arp daddr ip 192.168.50.1`.
- [ ] **Step 2: Run them.** Expected: FAIL (the stanza has no name line, the address is still `10.10.31.1/24`).
- [ ] **Step 3: Implement.**
  - Replace every `subnet(n)` / `f"{sub}.0/24"` / `f"{sub}.1"` with `nets.cidr(n)` / `nets.gateway(n)`. Stanza address: `f"{gateway}/{prefixlen}"`.
  - Route checks: match the whole network string, not the `10.10.N.` prefix.
  - The name line goes right after `block_begin`, only when `net_name(n) != f"net{n}"`. That keeps old blocks unchanged.
  - `next_ip` iterates `nets.vm_addresses(n)`, and on exhaustion dies with `f"{net_name(n)} ({cidr}) is full"`.
  - `ensure_dhcp_bridge` keeps `DHCP_NET` as is.
- [ ] **Step 4: Run the whole suite.** Expected: PASS. Run `uvx pyflakes src tests` with no output.
- [ ] **Step 5: Commit** `"nets: bridges, SDN vnets, isolation and VM IPs follow the net's CIDR"`

### Task 3: refuse a CIDR that collides with anything

**Files:**
- Modify: `src/pvelab/nets.py` (add `taken_ranges`), `src/pvelab/networks.py` (`check_free`, `foreign_use` use it)
- Test: `tests/unit/test_nets.py`

**Interfaces:**
- Consumes: Task 1 `overlap`, `all_nets`; `nodes.members()`, `nodes.run_on(node, *cmd, check=False, quiet=True)`, `config.SDN_SUBNETS`.
- Produces:
  - `nets.parse_routes(text: str) -> list[IPv4Network]`: `ip -4 route` output, without `default`, without host routes (/32)
  - `nets.taken_ranges(n: str) -> dict[str, IPv4Network]`: `"vmbr1"`, every other pvelab net by name, every SDN subnet not belonging to vnet `lab{n}` (label `"SDN subnet <id>"`), and every route on every online node except those on net n's own bridge (label `"route on <node>"`)
  - `nets.cidr_free(n: str, net: IPv4Network) -> None`: dies with `overlap(...)`'s message.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_routes`: on sample `ip -4 route` text with default, `10.0.0.0/24 dev vmbr0`, `10.10.40.0/24 dev lab40` and `169.254.0.0/16`, it returns the three non-default networks.
  - `test_route_on_the_other_host_is_refused`: fake `members()` for two online nodes and `run_on` returning `10.20.0.0/24 dev vmbr0` only for workshop2. `cidr_free("33", ip_network("10.20.0.0/23"))` dies with a message containing `route on workshop2` and `10.20.0.0/24`.
  - `test_offline_node_is_skipped_not_fatal`: the offline node's routes are not read, and the call still succeeds.
  - `test_own_bridge_route_does_not_count`: re-checking an existing net 31 against itself passes.
  - `test_list_warns_when_someone_overlaps_a_net`: with net 31 (`192.168.50.0/23`) existing and `taken_ranges("31")` faked to hold `{"SDN subnet office-192.168.51.0-24": ip_network("192.168.51.0/24")}`, `listing.summary` prints, in red, `docker-lab (192.168.50.0/23) overlaps SDN subnet office-192.168.51.0-24 (192.168.51.0/24): VMs may not reach each other or the internet`.
- [ ] **Step 2: Run.** Expected: FAIL (`taken_ranges` is not defined).
- [ ] **Step 3: Implement.**
  - Read the nodes in parallel with `util.parallel`. If a node does not answer, add a warning ("routes on workshop2 not checked: no answer") and do not die.
  - Add `nets.overlaps(n: str) -> list[str]`: every entry of `taken_ranges(n)` that overlaps `cidr(n)`, formatted as above.
  - `listing.summary` calls it for each net it prints: one read of routes and SDN subnets per `pvelab list`, shared by all nets (cache `taken_ranges`'s inputs for the call). A net with no overlap prints exactly as before.
  - Also modify `src/pvelab/commands/listing.py` (`summary`).
- [ ] **Step 4: Run the whole suite.** Expected: PASS.
- [ ] **Step 5: Commit** `"nets: a new net's CIDR must not overlap vmbr1, other nets, SDN subnets or any host's routes"`

### Task 4: names everywhere a net is shown or chosen

**Files:**
- Modify: `src/pvelab/networks.py:24` (`net_label` → `nets.net_name`), `src/pvelab/inventory.py` (`targets`: `--net NAME`; `sorted_nets` unchanged; `lab_data()["nets"]` gives names), `src/pvelab/commands/complete.py:74` (completes names), `src/pvelab/commands/listing.py`, `status.py`, `access.py`, `destroy.py`, `handout.py`, `src/pvelab/expose.py` (every `f"net{n}"` in output → `net_label(n)`), `src/pvelab/args.py` (new `net_arg`)
- Test: `tests/unit/test_nets.py`, `tests/unit/test_ux.py`

**Interfaces:**
- Consumes: Task 1 `resolve`, `net_name`, `cidr`.
- Produces: `args.net_arg(v: str) -> str`: `nets.resolve(v)` or dies with `f"no net called '{v}' (pvelab list shows the nets)"`. It replaces `valid_net` in `inventory.targets`, `listing`, `status`, `access` (expose/isolate/block). `create` keeps its own handling (Task 5).

- [ ] **Step 1: Write the failing tests.**
  - `test_targets_by_net_name`: managed VMs on net 31 are selected by `targets(["--net", "docker-lab"])`, and the same set by `["--net", "31"]`.
  - `test_unknown_net_name`: `targets(["--net", "nope"])` dies with `no net called 'nope'`.
  - `test_list_summary_line_names_the_range`: the `listing.summary` line is `docker-lab (192.168.50.0/23): students isolated from each other`. Old nets print `net40 (10.10.40.0/24): …`.
  - `test_completion_offers_names`: completing `--net` offers `docker-lab` and `net40`.
  - Existing `"net40 …"` assertions in other tests stay green, since the old default name is still `net40`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Hints that tell the user to run something (`pvelab isolate --net {n}`) print the name: `--net docker-lab`.
- [ ] **Step 4: Run the whole suite.** Expected: PASS.
- [ ] **Step 5: Commit** `"nets: --net takes a net's name; output names nets and shows their range"`

### Task 5: `pvelab create --net NAME --cidr CIDR`

**Files:**
- Modify: `src/pvelab/commands/create.py` (option parsing around line 208, plan text lines 86–88, net step 143–150, VM prefix line 251), `src/pvelab/networks.py` (`free_net`: an N is free when it has no block, no `labN`/`vmbrN` link or vnet, ignoring the 10.10.N route if a custom CIDR is used), `src/pvelab/commands/complete.py` (`--cidr` option)
- Test: `tests/unit/test_ux.py` (create's plan output is tested there)

**Interfaces:**
- Consumes: Tasks 1–4 (`register`, `name_problem`, `parse_cidr`, `cidr_free`, `net_name`).
- Produces: `create` accepts `--cidr CIDR`. The meaning of `--net V`:
  - `auto`: as today, a new N named `netN`.
  - A number or `netN`: as today.
  - An existing name: that net.
  - Any other valid name: a new net called that, with N from `free_net()`.

  `--cidr` is valid only when the net is new; otherwise it dies with `docker-lab exists with 192.168.50.0/23; --cidr only applies to a new net`. If `--cidr` matches the existing range, accept it silently. Without `--net`, `--cidr` dies with `--cidr needs --net NAME or --net auto`.

- [ ] **Step 1: Write the failing tests** (with `run`/`pvesh` faked like the existing create tests in `test_ux.py`).
  - `test_create_new_named_net_plan`: `create 3 debian13 --net docker-lab --cidr 192.168.50.0/23` plans `on docker-lab (192.168.50.0/23, lab2) - a new network` and VM names `docker-lab-student01..03`.
  - `test_create_more_on_named_net`: with a block for 31 named docker-lab, the next VM is `docker-lab-student04`, and no `--cidr` is needed.
  - `test_cidr_on_existing_net_refused`: see the message above.
  - `test_old_net_create_unchanged`: `--net 40` still gives `net40-student11` and the plan text `on net40 (10.10.40.0/24, lab40)`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Order in create:
  1. Resolve `--net`.
  2. For a new net, check `name_problem`, `parse_cidr` (default `default_cidr(N)`) and `cidr_free`.
  3. `nets.register(N, name, cidr)`.
  4. Then the existing planning.

  Every check runs before the plan is shown, so nothing is made on a refusal.
- [ ] **Step 4: Run the whole suite.** Expected: PASS.
- [ ] **Step 5: Commit** `"create: --net NAME --cidr CIDR makes a named net with its own range"`

### Task 6: wizard, help, `status` inside-ranges, README

**Files:**
- Modify: `src/pvelab/wizard.py:186-187` (ask "Network: a name for a new one, or a number/name of an existing one" and, for a new one, "Address range (CIDR)" defaulting to `10.10.N.0/24`, validated with `parse_cidr` + `cidr_free`), `src/pvelab/helptext.py` (create's `--net`/`--cidr` text, list/expose/isolate mention names), `src/pvelab/logins.py:24` (inside = the hosts' LAN, vmbr1, and `nets.all_nets()` ranges instead of the fixed `10.10.0.0/16`), `src/pvelab/config.py:61` (`INTERNAL_NETS` → only `10.0.0.0/24`), `README.md`
- Test: `tests/unit/test_status.py` (`test_inside_and_outside` gains `("192.168.50.20", True)` with net 31 present), `tests/unit/test_ux.py` (help text contains `--cidr`)

- [ ] **Step 1: Write the failing tests** described above.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Help for create, replacing the `--net N` paragraph's first line: `--net N|NAME  a class network: a number (10.10.N.0/24) or a name; a new name makes a new net`, and add `--cidr CIDR  address range of a new net (private, /16 to /27; default 10.10.N.0/24); VMs get .11 up`.
- [ ] **Step 4: Run the whole suite and pyflakes.** Expected: PASS, no output.
- [ ] **Step 5: Commit** `"nets: wizard, help and status know named nets and their ranges"`

### Task 7: live check on a self-test net

**Files:** none (verification only; fix in the owning task if it fails)

- [ ] **Step 1: Build and install for the owner's user only:** `python3 build.py && install -m 755 dist/pvelab ~/.local/bin/pvelab`. Do not write to `/usr/local/bin`.
- [ ] **Step 2: Create.** `sudo env LAB_YES=1 python3 ~/.local/bin/pvelab create 2 debian13 --net cidr-test --cidr 192.168.77.0/24 --node workshop1`. Expected: VMs `cidr-test-student01/02` with IPs `192.168.77.11/.12`, and the plan names `lab<N>`.
- [ ] **Step 3: Check.**
  - From student01: the internet answers (`curl -sI https://github.com`).
  - Student02's SSH does not answer (isolated).
  - `pvelab list --net cidr-test` shows the name and range.
  - `pvelab expose --net cidr-test`, then `pvelab expose --net cidr-test --off`, work.
  - `pvelab create 1 debian13 --net cidr-test2 --cidr 10.0.0.0/24` is refused with the hosts' route.
- [ ] **Step 3b: Check the pending-change guard.**
  1. Add a vnet without applying it: `pvesh create /cluster/sdn/vnets --vnet tstpend --zone pvelab --tag 4299999`.
  2. Run `pvelab create 1 debian13 --net cidr-test3 --cidr 192.168.78.0/24`. Expected: refused, naming `vnet tstpend (new)`, and nothing created.
  3. Remove the vnet with `pvesh delete /cluster/sdn/vnets/tstpend`. Then `pvesh get /cluster/sdn/vnets --pending 1` shows no `state` on any item.
- [ ] **Step 4: Clean up.** `pvelab destroy --net cidr-test` (LAB_YES=1); then `pvelab list` shows no `cidr-test`, `/etc/pve/sdn/subnets.cfg` has no `192.168.77.0`, and net30/net40 are untouched (still running, same IPs).
- [ ] **Step 5: Report the outputs to the owner.** No commit, unless a fix was made in an earlier task's files.
