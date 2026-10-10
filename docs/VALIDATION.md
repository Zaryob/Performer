# Local validation record

9 October 2026 (Europe/Istanbul); macOS 27.0 (26A428), Apple M4, 16 GiB RAM, Xcode 27.0 (27A266a), Apple Clang 21.0.0. Runs shared a busy development host; timings are observations, not performance guarantees.

Source baseline: [`c2e6dbd988129bf6833d440fd8d0d19de0c2f059`](https://github.com/Zaryob/Performer/commit/c2e6dbd988129bf6833d440fd8d0d19de0c2f059). No collector or viewer implementation changed in this documentation/demo branch.

| Check | Observed result |
| --- | --- |
| Synthetic steady/heavy bundle validation with `--verify-hashes` | Both passed, 12 files checked per bundle |
| Synthetic on-CPU diff | Completed; [example commands and hashes](examples/README.md) |
| Viewer `npm ci`, `npm test`, `npm run build` | Install/build succeeded; 117 tests passed, 1 skipped (8 files passed, 1 skipped) |
| Linux collector `make check` from a clean source archive | 432 tests run; 2 errors, 1 skipped; **not passing** |

Viewer environment: Node 26.9.0, npm 11.19.1, checked-in `package-lock.json`. The Linux test used Python 3.12 in a local Docker image, with the current Git source unpacked into a fresh container directory before building `tests/target/contention` there. It did not reuse macOS test executables.

Both Linux errors were in `StandardProfileTests`: `test_overhead_is_not_inflated_by_probe_startup` and `test_overhead_stays_under_the_profile_budget`. The measured overhead was `None`; comparison with the numeric budget raised `TypeError`. This container run cannot substantiate an overhead budget. [Full collector output](validation/collector-linux.txt) also retains subprocess resource warnings. Investigate environment prerequisites and measurement availability on a suitable Linux host; do not replace missing data with a successful budget assertion.

```sh
# Linux collector prerequisites are described in the main README.
make check
cd viewer
npm ci
npm test
npm run build
```

Synthetic data demonstrates the package/validation/viewer path only. It measures neither profiler overhead nor capture accuracy, eBPF behavior, perf permissions, production load or privacy of real profiles. Native macOS collector checks also failed because this collector depends on Linux facilities; macOS is not claimed as a supported collection host.

Remaining live measurement gates: [#8](https://github.com/Zaryob/Performer/issues/8). Existing [PR #6](https://github.com/Zaryob/Performer/pull/6) is independent and is not counted as this change's validation.

## Follow-up verification — 2026-10-10 (Istanbul)

The historical baseline above is preserved. Follow-up work was split into
commits for common collection windows, a real perf comparison, native symbol
and DWARF evidence, viewer controls/quality, and build provenance.

- Native ARM64 Linux: **529 tests passed, 3 skipped** (163.153 s).
- After the final module identity/loss parsing guard: **34 focused tests passed**,
  plus live mapped ELF build-ID and native perf.data loss parsing checks.
- Viewer: **133 tests passed**, TypeScript check and single-file build passed.
- Ubuntu 22.04 Docker: Debian artifact built, installed and packaged CLI plus
  synthetic PMU bundle smoke checks passed.
- GitHub's Ubuntu 22.04, Ubuntu 24.04 and offline viewer jobs passed for the
  feature commits; the final branch head is required to pass again before merge.
- Real 120-worker reference comparison: every worker appeared in both profilers
  within the shared 20 s gate; Performer also sampled main once (121 TIDs).
- Native DWARF: nested callchain retained in a target built without frame pointers.
- Integrated standard + PMU: certified 5 s gate, 17 measured PMU threads, module
  identity and original stack evidence retained; bundle hashes validated.

Details, reproduction and limits: [perf validation](perf-validation.md).
Concurrent profilers do not establish isolated collection overhead; the
controlled measurement below does. Browser visual verification was not performed.

## Overhead and robustness — 2026-10-10 (Istanbul)

Same Raspberry Pi 5 host. The native suite ran **549 tests, all passing
(1 skipped)**, with no ResourceWarnings. The helper sleepers are now reaped.
The viewer passed 133 tests, the type check and the build.

- The two budget tests now skip with the collector's own note when a host
  cannot measure overhead. A `None` estimate without a note fails. The
  `TypeError` is gone.
- [Alternating benchmark](validation/overhead-2026-10-10/README.md): 63 runs
  covering CPU-, lock- and I/O-bound workloads, each untraced, under
  Performer and under perf. All runs completed with no lost events.
  Performer cost −0.2%, −1.9% and −0.1% throughput, and +0.1%, +10.1% and
  +27.6% CPU per iteration.
- The benchmark found three collector defects, now fixed:
  - **futex:** an uncontended target's futex probe was disabled and the run
    marked partial.
  - **SIGTERM:** it left five bpftrace processes attached and no bundle.
  - **Overhead self-estimate:** the "untraced" CPU samples fell inside kernel
    probe teardown. The self-estimate is now 0.0% for cpu and 8.5% for lock
    (measured 10.1%).

Still open in issue #8: publishing verified sample bundles on a real release.
That step is deliberately left to the maintainer.
