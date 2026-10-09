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
