#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! -f $1 ]]; then
  echo "usage: $0 path/to/performer-collector.deb" >&2
  exit 2
fi

package=$(realpath "$1")
apt-get install -y "$package"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
performer --help >/dev/null
performer schema | grep -q 'manifest.schema.json'
performer fake-run --out "$tmp/runs" --label package-smoke
bundles=("$tmp"/runs/*.tgz)
performer validate --verify-hashes "${bundles[0]}"
