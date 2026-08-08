# tests/target/ — synthetic target

A workload whose bottleneck is known in advance, so the tool can be checked
for reaching the *right* conclusion rather than merely producing output.

```console
$ make -C tests/target
$ ./tests/target/contention --threads 315 --contention 90 --hold-us 50 --seconds 60 &
$ ./collector/bin/performer collect --pid $! --duration 30 --label contended
```

| flag | effect |
|---|---|
| `--threads N` | worker thread count — use 315 to reproduce the real case |
| `--contention P` | percent of iterations that take the single shared mutex |
| `--hold-us U` | microseconds held inside that critical section |
| `--sleep-us U` | microseconds slept per iteration (off-CPU / timer pressure) |
| `--work N` | units of pure user-space CPU per iteration |
| `--churn-ms N` | create a short-lived thread every N milliseconds; 0 disables it |
| `--seconds S` | run time; 0 means run until killed |

Two configurations matter most:

* `--contention 90 --hold-us 50` — one mutex dominates. M5's acceptance
  criterion is that the tool puts *that* mutex first.
* `--contention 0 --sleep-us 1000` — threads are almost entirely off-CPU and
  no lock should be blamed. A tool that reports contention here is wrong.

Built with `-fno-omit-frame-pointer` deliberately: it is also the fixture for
checking that the preflight frame pointer check passes when it should.
