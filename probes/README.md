# probes/ — bpftrace programs

The full set from the specification, all landed as of M2.

Probes take positional parameters: `$1` is the target pid, `$2` the watchdog in
seconds, and any further `$N` are thresholds supplied by the profile.

| file | what it answers | tier |
|---|---|---|
| `oncpu.bt` | where CPU time goes (`profile:hz:99`) | light |
| `runqlat.bt` | how long runnable threads wait for a CPU | light |
| `threadlife.bt` | thread creation and exit, and lifetimes | light |
| `offcpu.bt` | where threads block, and for how long | standard |
| `futex.bt` | lock contention, aggregated per `uaddr` | standard |
| `wakeup.bt` | who wakes whom — the thread interaction graph | standard |
| `syscall_lat.bt` | time per syscall | deep |
| `timers.bt` | timer arm/wait rate per call site | deep |

## Conventions

* **Prefer tracepoints over kprobes.** They survive kernel upgrades; a kprobe
  on an inlined or renamed function does not.
* **Sample at 99 Hz, not 100.** A target full of timers can lock onto a round
  sampling frequency and produce a systematically wrong profile.
* **`sched_switch` records the stack when a thread goes off-CPU** and closes
  the interval when it comes back. This is correct because a sleeping thread
  cannot change its own stack.
* **Do not filter `prev_state`.** A futex wait is `TASK_INTERRUPTIBLE`; a
  `--state=2`-style filter deletes exactly the contention being hunted. The
  state is recorded as a label and the viewer filters on it.
* **Aggregate by address where there is one.** `futex_by_addr` is what turns
  "futex time is high" into "*this* mutex is hot".
* **Record numbers, resolve names later.** `syscall_lat.bt` stores the syscall
  number; carrying a string per event would cost far more than the mapping
  costs in post-processing.
* **Every probe carries an `interval:s:$2` watchdog**, so a collector that
  dies does not leave the target traced forever.
* **Every probe's stderr goes to `raw/<probe>.stderr.log`.** Never
  `2>/dev/null`: it is the only place an attach failure or a lost event count
  is reported.
* **bpftrace dumps its maps on SIGINT and at no other time.** A probe that
  does not receive SIGINT produces nothing, however long it ran.
* **Clear scratch maps in `END`.** Per-thread bookkeeping maps would otherwise
  be printed alongside the results and parsed as data.

## Costs

`offcpu` and `futex` fire on every context switch and every futex call
respectively, which on a process with hundreds of threads is a very large
number of events. Both carry a threshold from the profile so the short
intervals -- which say nothing and would crowd out the ones that matter --
never reach a map. `syscall_lat` has no such filter and belongs to short runs
only; it is why the `deep` tier is capped at 60 seconds.
