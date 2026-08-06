# probes/ — bpftrace programs

`oncpu.bt` landed in M1; the rest arrive in M2.

Probes take positional parameters: `$1` is the target pid, `$2` the watchdog in
seconds, and any further `$N` are thresholds from the profile.

| file | what it answers | milestone |
|---|---|---|
| `oncpu.bt` | where CPU time goes (`profile:hz:99`) | **done (M1)** |
| `offcpu.bt` | where threads block, and for how long | M2 |
| `runqlat.bt` | scheduler queueing delay | M2 |
| `futex.bt` | lock contention, aggregated per `uaddr` | M2 |
| `wakeup.bt` | who wakes whom — the thread interaction graph | M2 |
| `syscall_lat.bt` | time per syscall | M2 (deep) |
| `threadlife.bt` | thread creation and exit | M2 |
| `timers.bt` | timer arm/disarm rate | M2 (deep) |
| `uprobe.bt` | application-level queue depth and dispatch latency | M2 (deep) |

Conventions to hold to when they land:

* Prefer tracepoints over kprobes — they survive kernel upgrades.
* `sched_switch` records the stack when a thread goes off-CPU and closes the
  interval when it comes back; the stack cannot change while it sleeps, so this
  is correct.
* Do **not** filter `prev_state`. A futex wait is `TASK_INTERRUPTIBLE`; a
  `--state=2`-style filter deletes exactly the contention being hunted. Record
  the state as a label and let the viewer filter.
* Every probe's stderr goes to `raw/<probe>.stderr.log`. Never `2>/dev/null`.
* bpftrace dumps its maps on SIGINT and at no other time. A probe that does not
  receive SIGINT produces nothing. Every probe also carries an `interval:s:$2`
  watchdog so that a collector which dies does not leave the target traced
  forever.
* Sample at 99 Hz, not 100: a target full of timers can otherwise lock onto the
  sampling frequency and produce a systematically wrong profile.
