#!/usr/bin/env bash
set -euo pipefail

source_dir="${1:-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_root/manifest.json"

if [[ -z "$source_dir" ]]; then
	echo "Usage: $0 <fanchmwrt-source-directory>" >&2
	exit 2
fi
if ! git -C "$source_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	echo "Not a Git working tree: $source_dir" >&2
	exit 2
fi
if [[ ! -f "$manifest" ]]; then
	echo "Backport manifest is missing: $manifest" >&2
	exit 1
fi

readarray -t manifest_values < <(
	python3 - "$manifest" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    manifest = json.load(stream)
print(manifest["fanchmwrt"]["base_commit"])
print(manifest["patch_count"])
PY
)
base_commit="${manifest_values[0]:-}"
expected_count="${manifest_values[1]:-}"
base_commit="${base_commit%$'\r'}"
expected_count="${expected_count%$'\r'}"
source_commit="$(git -C "$source_dir" rev-parse HEAD)"

if [[ ! "$base_commit" =~ ^[0-9a-f]{40}$ ]] ||
	[[ ! "$expected_count" =~ ^[1-9][0-9]*$ ]]; then
	echo "Backport manifest contains invalid compatibility data" >&2
	exit 1
fi
if [[ "$source_commit" != "$base_commit" ]]; then
	echo "Unsupported FanchmWrt source revision: $source_commit" >&2
	echo "This patch set requires: $base_commit" >&2
	exit 1
fi
if ! git -C "$source_dir" diff --cached --quiet; then
	echo "Refusing to apply device backports over staged source changes" >&2
	exit 1
fi

mapfile -d '' patches < <(
	find "$repo_root/patches" -maxdepth 1 -type f -name '*.patch' -print0 | sort -z
)
if (( ${#patches[@]} != expected_count )); then
	echo "Patch count mismatch: expected $expected_count, found ${#patches[@]}" >&2
	exit 1
fi

applied=()
rollback() {
	local index
	trap - ERR INT TERM
	for (( index=${#applied[@]} - 1; index >= 0; index-- )); do
		git -C "$source_dir" apply --reverse "${applied[index]}" || true
	done
}
abort_apply() {
	local status=$?
	(( status != 0 )) || status=1
	rollback
	exit "$status"
}
trap abort_apply ERR INT TERM

for patch in "${patches[@]}"; do
	echo "Applying ${patch##*/}"
	git -C "$source_dir" apply --check --whitespace=nowarn "$patch"
	git -C "$source_dir" apply --whitespace=nowarn "$patch"
	applied+=("$patch")
done

trap - ERR INT TERM
echo "Applied ${#patches[@]} device backports to $source_commit"
