# collector/profiles/ — overhead tiers

Tracing every context switch and every syscall of a process with hundreds of
threads perturbs it badly enough to invalidate the measurement, so collection
comes in three tiers. Each is a YAML file naming its probes, their thresholds
and a maximum duration.

| profile | probes | expected overhead | max duration | use |
|---|---|---|---|---|
| `light` | oncpu, runqlat, threadlife + `/proc` series | < 3% | 900 s | first look |
| `standard` | + offcpu (100 µs), futex (50 µs), wakeup | 5–15% | 300 s | default diagnosis |
| `deep` | + syscall_lat, timers | 20–60% | 60 s | hypothesis testing |

The time budget runs the opposite way from the overhead, on purpose: the more
a tier costs the target, the less of the target's life it is allowed to spend.
A longer run than the tier permits is refused unless `--force` is given.

## Writing a profile

```yaml
name: mine          # must match the file name; the manifest records this
description: "why this set of probes"
expected_overhead: "5-15%"
max_duration_s: 300

probes:
  - name: futex     # must have an emitter in performer/emit.py
    program: futex.bt
    required: false # true means its failure fails the whole run
    thresholds:
      min_us: 50    # becomes $3, and is recorded in the manifest
```

Thresholds are passed to the `.bt` program as positional parameters after the
pid and the watchdog, and are copied into `manifest.probes[].thresholds`
because they change what the numbers mean: "no lock waited more than 50 µs" and
"no lock waits were recorded" are very different findings.

Unknown keys are an error rather than being ignored — the likeliest cause is a
typo in a threshold, and a silently dropped threshold would quietly change the
measurement.

Profile names are a whitelist, not paths: they match
`^[a-z0-9][a-z0-9_-]{0,31}$` and resolve against this directory only. From M6
the name is the only probe-selecting input the daemon accepts from the network.

The reader is `performer/yamlish.py`, a small YAML subset — the collector may
not depend on anything outside the standard library, so PyYAML is not an
option. Anchors, aliases, flow collections and block scalars are refused with a
line number rather than half-parsed.
