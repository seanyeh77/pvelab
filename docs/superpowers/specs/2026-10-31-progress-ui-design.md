# Live progress for each VM, and one consistent flow for every command: design

Date: 2026-10-31. Status: agreed in chat ("要，請開始實作").

## Why

When a command works on many VMs, the instructor cannot see where each VM is. The line `1/20 done, 17 running (a, b, c +14)` does not say which stage a VM is in, how long it has been there, or whether it is stuck.

`node add` already has the shape the instructor likes:
- numbered steps
- a spinner line with a dim live tail
- one confirmation with a plan
- a summary at the end, with the next steps and a log path

It follows the bash-terminal-progress-ui technique, done in Python.

## Goals

1. **A live per-VM board.** It is one block of lines that is redrawn in place about every 0.2 s.
   - **A header line:** the step title, then `done / failed / total`, then the elapsed time.
   - **One line per VM:**
     - a spinner, or ✔ / ✘
     - the name
     - the host (only when there are 2 or more hosts)
     - the stage
     - the time spent in that stage
     - dim: the latest line the VM printed
   - **The latest line refreshes every few seconds.** It comes from the guest agent:
     - first boot: `tail -n1 /var/log/cloud-init-output.log`
     - scripts: their output, which pvelab now also writes to a file in the VM.
   - **Before the agent answers,** the line shows `(no guest agent yet)`.
   - **A stuck VM is marked.** If a VM's latest line has not changed for 2 minutes, its line turns yellow and adds `no output for M:SS`.
   - **When there are more VMs than the window has rows:**
     - failed VMs stay on top
     - finished VMs collapse into one line, `✔ N ready`
     - running VMs fill the space that is left
     - the rest becomes `… N more`.
   - **Width and height are measured again while drawing.** Every part that varies is cut to fit, so a window that shrinks does not fill with copies.
   - **Without a terminal** (a pipe, a file, CI), there is no animation. Each VM prints one plain line when it changes stage.
2. **Stages, by command:**
   - **create:** queued → clone → first boot → snapshot clean → script k/n NAME → snapshot NAME → ready
   - **run:** waiting for agent → script k/n NAME → snapshot NAME → done
   - **start / reboot:** start or reboot → booting, until the guest agent answers (up to a few minutes; the VM itself is never stopped) → running
   - **stop:** shutting down → stopped
   - **snapshot, rollback, destroy, resize:** one stage each, shown on the board. VMs run in parallel with a small pool.
   - **A template copy** (in create) shows a spinner line with the copy's output as its tail.
3. **Numbered steps** `[k/n] title` in create, run and destroy, and in any command with more than one phase.
4. **One confirmation, with a plan.**
   - Before anything changes, a command lists `This will: 1. … 2. …`, then every warning together, then asks once.
   - In create the warnings are:
     - the expose-without-harden warning
     - the resource warnings for each host
     - the template copies (size, and to which host)
   - **The skip flags keep their meaning:**
     - LAB_FORCE=1 skips resource warnings.
     - LAB_YES=1 skips the others.
     - Without a terminal, the command goes on only if every warning shown has its flag set.
     - Otherwise it stops and names the flag.
   - destroy and rollback already ask; they get the same plan format.
5. **A summary at the end.** It gives:
   - the result: per VM where it matters, or counts
   - the failed VMs with their log paths
   - the next commands to run, for example `pvelab list --net 40 --passwords` and `pvelab handout --net 40`
   - the log path.
6. **One log per command run,** at `/root/lab/logs/<command>-<time>.log`. It records:
   - every stage change
   - every latest line
   - the output of the commands pvelab runs.

   The existing per-VM script logs in `run-logs/` stay.

## Non-goals

- No new commands.
- No change to what commands do, apart from these:
  - start and reboot now wait for the guest agent (bounded).
  - snapshot, rollback, destroy and resize run with a small parallel pool.
  - Scripts' stdout and stderr are merged in the per-VM log, so the latest line can be read live.
- No curses or third-party library: ANSI codes only, as in the bash technique.

## Anti-goals

- **Hiding a failure behind a happy spinner.** A failed or stuck VM must stand out: ✘ in red, yellow for stuck.
- **Screen corruption** when the window is resized during a run, and a cursor that stays hidden after Ctrl-C.
- **Losing information that the output gives today.** It moves to the summary or the log.
- **Breaking `tests/selftest.sh`.** Its greps on output must be updated together with the code.

## Left to the builder

- The module layout, for example `ui.Board` and a `Plan` helper.
- Polling intervals and pool sizes.
- Exact wording.
