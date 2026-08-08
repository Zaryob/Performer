#!/bin/sh
set -eu

target_pid_file=${TARGET_PID_FILE:-/coord/target.pid}
expected_workers=${EXPECTED_WORKERS:-16}
wait_timeout=${TARGET_WAIT_TIMEOUT:-30}
duration=${COLLECT_DURATION:-10}
label=${PERFORMER_LABEL:-docker-demo}
profile=${PERFORMER_PROFILE:-light}
performer=/opt/performer/collector/bin/performer

target_pid=

cleanup() {
    status=$?
    trap - EXIT INT TERM
    if [ -n "$target_pid" ]; then
        kill -TERM "$target_pid" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM

mount_kernel_fs() {
    fs_type=$1
    mount_point=$2
    if ! grep -qs " $mount_point $fs_type " /proc/mounts; then
        mount -t "$fs_type" "$fs_type" "$mount_point"
    fi
}

# A privileged container gets the BPF syscalls, but tracefs/debugfs are not
# necessarily mounted in its mount namespace (notably on Docker Desktop).
mount_kernel_fs tracefs /sys/kernel/tracing
mount_kernel_fs debugfs /sys/kernel/debug

deadline=$((wait_timeout * 5))
attempt=0
while [ "$attempt" -lt "$deadline" ]; do
    if [ -r "$target_pid_file" ]; then
        candidate=$(sed -n '1p' "$target_pid_file")
        case "$candidate" in
            ''|*[!0-9]*) candidate= ;;
        esac
        if [ -n "$candidate" ] && [ -r "/proc/$candidate/comm" ]; then
            comm=$(sed -n '1p' "/proc/$candidate/comm")
            if [ "$comm" = contention ]; then
                target_pid=$candidate
                thread_count=$(find "/proc/$target_pid/task" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
                if [ "$thread_count" -ge $((expected_workers + 1)) ]; then
                    break
                fi
            fi
        fi
    fi
    attempt=$((attempt + 1))
    sleep 0.2
done

if [ -z "$target_pid" ] || [ ! -d "/proc/$target_pid" ]; then
    echo "performer demo: target PID could not be discovered within ${wait_timeout}s" >&2
    exit 1
fi

thread_count=$(find "/proc/$target_pid/task" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
if [ "$thread_count" -lt $((expected_workers + 1)) ]; then
    echo "performer demo: target reached only $thread_count threads; expected at least $((expected_workers + 1))" >&2
    exit 1
fi

echo "performer demo: collecting PID $target_pid ($thread_count threads) for ${duration}s"
"$performer" collect \
    --pid "$target_pid" \
    --duration "$duration" \
    --profile "$profile" \
    --label "$label" \
    --tag docker \
    --notes "Collected from the Docker Compose contention demo" \
    --out /runs \
    --overhead-window 1

bundle=$(find /runs -maxdepth 1 -type f -name "performer-*-${label}.tgz" -print | sort | tail -n 1)
if [ -z "$bundle" ]; then
    echo "performer demo: collection finished but no bundle was found" >&2
    exit 1
fi

"$performer" validate --verify-hashes "$bundle"
run_dir=$(find /runs -maxdepth 1 -type d -name "run_*_${label}" -print | sort | tail -n 1)
chmod a+rw "$bundle"
if [ -n "$run_dir" ]; then
    chmod -R a+rwX "$run_dir"
fi
echo "performer demo: bundle ready at $bundle"
