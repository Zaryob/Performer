#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

if [[ $# -ne 1 || ( $1 != 22.04 && $1 != 24.04 ) ]]; then
  echo "usage: $0 22.04|24.04" >&2
  exit 2
fi

source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != "$1" ]]; then
  echo "build this package on Ubuntu $1 (found $ID $VERSION_ID)" >&2
  exit 2
fi

command -v dpkg-deb >/dev/null || { echo "dpkg-deb is required" >&2; exit 2; }
version=$(PYTHONPATH=collector python3 -c 'from performer import __version__; print(__version__)')
package_version="${version}+ubuntu${VERSION_ID}"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
payload="$stage/usr/share/performer"

while IFS= read -r -d '' file; do
  install -Dm644 "$file" "$payload/$file"
done < <(find collector/performer -type f -name '*.py' -print0)
install -Dm755 collector/bin/performer "$payload/collector/bin/performer"
for file in collector/profiles/*.yaml probes/*.bt schema/*.json; do
  install -Dm644 "$file" "$payload/$file"
done
install -Dm644 LICENSE "$stage/usr/share/doc/performer-collector/copyright"
install -d "$stage/usr/bin" "$stage/DEBIAN"
ln -s /usr/share/performer/collector/bin/performer "$stage/usr/bin/performer"

cat > "$stage/DEBIAN/control" <<EOF
Package: performer-collector
Version: $package_version
Section: utils
Priority: optional
Architecture: all
Maintainer: Suleyman Poyraz <zaryob.dev@gmail.com>
Depends: python3 (>= 3.8), bpftrace (>= 0.14)
Description: Perf and eBPF collector for Linux processes
 Performer records CPU time, scheduling, and lock contention in portable
 run bundles that can be opened with the offline viewer.
EOF

mkdir -p dist
artifact="dist/performer-collector_${package_version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$artifact"
echo "$artifact"
