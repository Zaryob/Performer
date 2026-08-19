#!/bin/sh
set -eu

# Both Compose services use the host PID namespace. The exec keeps this PID
# unchanged, so the value written here is also the PID seen by eBPF probes.
pid_file=/coord/target.pid
temporary="${pid_file}.tmp.$$"
printf '%s\n' "$$" > "$temporary"
mv "$temporary" "$pid_file"

exec /usr/local/bin/contention "$@"

