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
Concurrent profilers do not establish isolated collection overhead. The old
ResourceWarnings in daemon test subprocess helpers remain visible in the native
suite log. Browser visual verification was not performed. Broader workload and
controlled overhead requirements in issue #8 remain open.
