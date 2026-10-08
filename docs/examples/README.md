# Synthetic bundle pair

These are generated fixtures, **not captured user profiles** and not evidence of collector overhead/accuracy. Both packages were generated on 9 October 2026 (Europe/Istanbul; archive timestamps may be 8 October UTC) using source baseline `c2e6dbd988129bf6833d440fd8d0d19de0c2f059`.

From the repository root, use the collector module without installing a global command:

```sh
PYTHONPATH=collector python3 -m performer fake-run --out demo --label steady --duration 60 --load steady --seed 4242
PYTHONPATH=collector python3 -m performer fake-run --out demo --label heavy --duration 90 --load heavy --seed 4242
PYTHONPATH=collector python3 -m performer validate --verify-hashes docs/examples/steady.tgz
PYTHONPATH=collector python3 -m performer validate --verify-hashes docs/examples/heavy.tgz
PYTHONPATH=collector python3 -m performer diff docs/examples/steady.tgz docs/examples/heavy.tgz --kind oncpu
```

Both committed archives passed validation with 12 hashed files each. [SHA256SUMS.txt](SHA256SUMS.txt) identifies these exact artifacts; [diff.txt](diff.txt) is their observed comparison. The generated filename includes a timestamp: use the files printed by `fake-run` when comparing newly generated data. Seeds control synthetic content, but timestamps/package metadata mean regenerated archives need not have the same byte hash.

Open the bundles in the viewer using its file picker. No real capture, network access or privileged Linux probe is needed to explore this pair. See [validation scope](../VALIDATION.md) for the separate failed live overhead checks.
