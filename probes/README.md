# probes/ — bpftrace programs

Empty until **M2** (with `oncpu.bt` arriving in M1). Nothing here yet, on
purpose: M0 fixes the bundle format, and probes that write into an unsettled
format would have to be rewritten.

Planned set, from the specification:

| file | what it answers | milestone |
|---|---|---|
| `oncpu.bt` | where CPU time goes (`profile:hz:99`) | M1 |
| `offcpu.bt` | where threads block, and for how long | M2 |
| `runqlat.bt` | scheduler queueing delay | M2 |
| `futex.bt` | lock contention, aggregated per `uaddr` | M2 |
| `wakeup.bt` | who wakes whom — the thread interaction graph | M2 |
| `syscall_lat.bt` | time per syscall | M2 (deep) |
| `threadlife.bt` | thread creation and exit | M2 |
| `timers.bt` | timer arm/disarm rate | M2 (deep) |
| `uprobe.bt` | application-level queue depth and dispatch latency | M2 (deep) |

Conventions to hold to when they land:

* `$1` is the target pid, `$2` a threshold in microseconds.
* Prefer tracepoints over kprobes — they survive kernel upgrades.
* `sched_switch` records the stack when a thread goes off-CPU and closes the
  interval when it comes back; the stack cannot change while it sleeps, so this
  is correct.
* Do **not** filter `prev_state`. A futex wait is `TASK_INTERRUPTIBLE`; a
  `--state=2`-style filter deletes exactly the contention being hunted. Record
  the state as a label and let the viewer filter.
* Every probe's stderr goes to `raw/<probe>.stderr.log`. Never `2>/dev/null`.
* bpftrace dumps its maps on SIGINT and at no other time. A probe that does not
  receive SIGINT produces nothing.
