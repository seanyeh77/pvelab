# pvelab status: hosts, students' connections, and login attempts

Date: 2026-10-31. Status: agreed in chat.

## Why

During class, the instructor wants to see three things in one place:

- **Hosts and cluster:** are the hosts and the links between them healthy?
- **Students:** who is logged in to which VM, from where, and whether each VM is online.
- **Security:** is anyone trying to log in right now?

That last point is real. On 2026-10-31 the journal on workshops showed external IPs (77.239.124.41, 77.239.124.69) guessing root's ssh password every few seconds. In October, students' VMs were compromised by a miner.

## Goals

1. **Two modes.**
   - `pvelab status [--net N] [--since 1h]` prints the current state once.
   - `pvelab status -f` keeps a full screen open and refreshes it every few seconds, like `top`, until Ctrl-C.
   - It is read-only. It needs root to read journals and talk to the guest agents, but it takes no lock, so it runs during a create.
2. **Hosts and cluster.** One line per host:
   - online or offline
   - its corosync link to each other host: connected or down, from `corosync-cfgtool -s` / `-n`
   - the round-trip time between the hosts
   - whether VXLAN works between the hosts, if this can be tested cheaply. Otherwise it shows the zone's peers and the UDP 4789 result from the last check.

   Then the cluster: quorate or not, votes, expected votes, and whether a QDevice is set up.
3. **Students' VMs.** One line per VM that is in use:
   - who is logged in: user, source IP and how long. From `who` / `ss` inside the VM, through the guest agent.
   - the number of ssh sessions
   - whether it can reach the internet. This is a cheap check, done at most once per refresh cycle per VM.

   Further rules:
   - VMs with nobody logged in collapse into one line, for example `38 VMs: nobody logged in`.
   - Stopped VMs and VMs whose agent does not answer are counted, with their names abbreviated.
   - With 2 or more hosts, each VM line shows its host.
4. **Security: login attempts** in the window given by `--since` (default the last hour).
   - **Each host's sshd:** the number of failed attempts, the top source IPs with their counts, and the user names tried.
   - **Each host's web UI and API (8006):** failed logins (pvedaemon "authentication failure"), with source IPs.
   - **Students' VMs whose ssh is exposed** (`pvelab expose`): failed ssh attempts and top source IPs, read from each VM's journal through the agent.
   - **Inside and outside are kept apart:**
     - Internal sources (10.0.0.0/24, the lab nets 10.10.0.0/16, the hosts) are listed apart from outside IPs.
     - A failure from a student's own IP counts as a student typing a wrong password, not as an attack.
   - **Thresholds** (pvelab's call, adjustable later):
     - An outside IP with 20 or more failures in the window is highlighted.
     - A host or VM with 100 or more failures per hour is red.
     - A successful login from an IP that also failed many times is red and called out. This may be a guessed password.
5. **Output style** is the same as the rest of pvelab:
   - colours only on a terminal
   - in `-f` mode, the screen is redrawn in place, the width and height are measured each frame, and the content is cut to fit
   - without a terminal, plain text.
6. **Hints only, no actions.**
   - When there are many outside attempts, one line suggests what could be done: key-only root login, a security group rule, fail2ban.
   - It changes nothing.

## Non-goals

- Blocking IPs or installing fail2ban. The instructor has not decided on hardening.
- History beyond the journals that already exist. No database, no daemon.
- Alerts, such as mail or chat messages.

## Anti-goals

- **Making the class slower.**
  - The agent calls run in parallel, with timeouts, and each VM gets at most one or two calls per cycle.
  - In `-f` mode, the cycle is about 5–10 s for VM data. Journals are read incrementally or with `--since`, and never re-read in full each frame.
- **Showing a student's password, or anything else from credentials.tsv,** on the status screen.
- **Taking the lock, or failing during another command.**

## Constraints and facts

- **Hosts:** workshops (10.0.0.123) and pve9 (10.0.0.229), running PVE 9.2. Their sshd logs to the journal as `sshd-session` lines, for example `Failed password for root from IP`, `Invalid user X from IP`, `Accepted password for … from IP`.
- **VMs:**
  - They are Debian 13 or Ubuntu cloud images.
  - The guest agent `exec` runs commands, and `nodes.on_node`/`guest_json` already route to the VM's host.
  - The ssh unit is called `ssh` on Debian and Ubuntu.
- **Remote hosts** are reached with `nodes.run_on` (root ssh inside the cluster).
- **Existing helpers to reuse:**
  - `inventory`, `cred_rows` (for IPs only)
  - `vm_statuses`, `guest_json`, `parallel`
  - `members`, `multi`
  - `expose.forwarded`
  - `ui.paint` / `fit` / `ANSI`
  - `top.py`'s full-screen loop. If it is reusable, use it.

## Left to the builder

- The module layout and the exact columns.
- How to get the round-trip time and the VXLAN check cheaply.
- The parsing details for each log format.
- How often each data source refreshes in `-f`.
