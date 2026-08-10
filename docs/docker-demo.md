# Docker Compose demo

The demo runs a known, mutex-heavy C++ workload and attaches Performer from a
second container. The resulting bundle is written to `runs/` on the host.

## Run a real collection

The collector loads eBPF programs into the Docker host's Linux kernel, so it
must be privileged and must share the host PID namespace. Run:

```console
$ make docker-demo
```

This builds both images, starts 16 contending worker threads, collects the
`standard` profile for 10 seconds, validates the archive and stops the target.
The bundle can then be opened in `viewer/dist/index.html`.

The useful knobs are Compose environment variables:

```console
$ DEMO_THREADS=64 COLLECT_DURATION=20 PERFORMER_PROFILE=standard make docker-demo
```

| variable | default | meaning |
|---|---:|---|
| `DEMO_THREADS` | `16` | target worker count |
| `DEMO_CONTENTION` | `90` | iterations taking the shared mutex, percent |
| `DEMO_HOLD_US` | `50` | time spent holding that mutex |
| `DEMO_SLEEP_US` | `200` | sleep after each iteration |
| `DEMO_WORK` | `200` | CPU work for non-contending iterations |
| `DEMO_CHURN_MS` | `250` | interval between short-lived demo threads |
| `COLLECT_DURATION` | `10` | measured seconds, excluding preflight |
| `PERFORMER_PROFILE` | `standard` | `light`, `standard` or `deep` |
| `PERFORMER_LABEL` | `docker-demo` | bundle label |
| `PERFORMER_RUNS_DIR` | `./runs` | host output directory |

The default command uses `--abort-on-container-exit` and returns the collector
exit code. To inspect service logs or control the lifecycle manually:

```console
$ docker compose up --build
$ docker compose down
```

### Kernel requirements

This is a Linux-kernel integration test, not only an image build. The Docker
host must allow privileged containers, eBPF and perf events. It works on a
native Linux Docker Engine and can work on Docker Desktop through its Linux
VM; rootless Docker and restricted CI runners generally cannot perform the
real collection.

The target and collector deliberately use `pid: host`. Kernel trace events
carry host-namespace PIDs, so passing a container-local PID would attach the
probes but filter out the target's events.

## Smoke-test without eBPF

To verify the image, collector CLI, schemas and output mount on a restricted
machine, generate and validate a synthetic bundle instead:

```console
$ make docker-fake
```

This path is unprivileged and does not start the C++ target. It does not test
bpftrace or kernel access.
