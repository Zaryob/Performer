# collector/profiles/ — overhead tiers

Empty until **M2**. Tracing every `sched_switch` and every syscall of a
315-thread process perturbs it badly, so collection comes in three tiers, each
a YAML file naming its probes, thresholds and a recommended maximum duration.

| profile | probes | expected overhead | use |
|---|---|---|---|
| `light` | oncpu, runqlat, threadlife, `/proc` series | < 3% | first look |
| `standard` | + offcpu (100 µs), futex (50 µs), wakeup | 5–15% | default diagnosis |
| `deep` | + syscall_lat, timers, uprobe | 20–60% | hypothesis testing, short runs |

`deep` runs longer than 60 s must be refused unless `--force` is given.

Profile names are a whitelist, not paths: they match `^[a-z0-9][a-z0-9_-]{0,31}$`
and are the only probe-selecting input the M6 daemon will accept from the
network.
