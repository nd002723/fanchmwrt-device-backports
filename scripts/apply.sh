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
# The series is pinned to one released tag, but it also has to run on the
# maintenance branch that is built on top of it: fanchmwrt keeps its private
# layer (fwxd, the luci-app-fwx-* set, the matching feeds.conf entry) on
# fanchmwrt-<tag>, a branch that only adds to the released tree. Accept the pin
# itself and any commit that descends from it, because "the branch head moved"
# must not make every device backport unusable. Being a descendant is not a
# promise that the patches still fit - each one is checked and applied below,
# and any failure rolls the whole series back.
if [[ "$source_commit" != "$base_commit" ]]; then
	if ! git -C "$source_dir" cat-file -e "${base_commit}^{commit}" 2>/dev/null; then
		cat >&2 <<-EOF
		Cannot verify the FanchmWrt source revision: the clone is too shallow
		  source:        $source_commit
		  required base: $base_commit
		Check the upstream source out with full history (actions/checkout
		"fetch-depth: 0"), or run "git fetch --unshallow" in $source_dir.
		EOF
		exit 1
	fi
	if ! git -C "$source_dir" merge-base --is-ancestor "$base_commit" "$source_commit" 2>/dev/null; then
		echo "Unsupported FanchmWrt source revision: $source_commit" >&2
		echo "This patch set requires $base_commit or a commit that descends from it" >&2
		exit 1
	fi
	echo "Source revision $source_commit descends from the ${base_commit:0:12} baseline"
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

# A maintenance branch reworks the very board lists these patches touch (extra
# device names in 02_network, extra cases in 11_fix_wifi_mac), so the context
# drifts by a line or two and git apply refuses a patch that is still perfectly
# meaningful. GNU patch tolerates that drift through bounded fuzz. Only enable it
# when the series runs on such a descendant: on the pinned tag the patches must
# still match exactly, and a failure there stays a hard failure.
fuzz_limit=0
if [[ "$source_commit" != "$base_commit" ]]; then
	fuzz_limit=3
fi

applied=()
loosened=0
rollback() {
	local index entry method patch
	trap - ERR INT TERM
	for (( index=${#applied[@]} - 1; index >= 0; index-- )); do
		entry="${applied[index]}"
		method="${entry%%:*}"
		patch="${entry#*:}"
		if [[ "$method" == git ]]; then
			git -C "$source_dir" apply --reverse "$patch" || true
		else
			(
				cd "$source_dir" &&
					patch -p1 --reverse --forward --no-backup-if-mismatch \
						--fuzz="${method#fuzz}" --silent <"$patch"
			) || true
		fi
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
	name="${patch##*/}"
	echo "Applying $name"
	if git -C "$source_dir" apply --check --whitespace=nowarn "$patch" 2>/dev/null; then
		git -C "$source_dir" apply --whitespace=nowarn "$patch"
		applied+=("git:$patch")
		continue
	fi
	if (( fuzz_limit == 0 )); then
		# Report git's own diagnosis, then let the ERR trap roll the series back.
		git -C "$source_dir" apply --check --whitespace=nowarn "$patch"
		git -C "$source_dir" apply --whitespace=nowarn "$patch"
	fi

	matched=0
	for fuzz in $(seq 1 "$fuzz_limit"); do
		if (
			cd "$source_dir" &&
				patch -p1 --fuzz="$fuzz" --forward --no-backup-if-mismatch \
					--dry-run --silent <"$patch" >/dev/null 2>&1
		); then
			if (
				cd "$source_dir" &&
					patch -p1 --fuzz="$fuzz" --forward --no-backup-if-mismatch \
						--silent <"$patch"
			); then
				echo "  context loosened: fuzz $fuzz"
				applied+=("fuzz${fuzz}:$patch")
				loosened=$(( loosened + 1 ))
				matched=1
				break
			fi
		fi
	done
	if (( matched == 0 )); then
		echo "Cannot apply $name even with fuzz $fuzz_limit" >&2
		false
	fi
done

trap - ERR INT TERM

# A partially applied series is worse than a refused one: the tree looks patched
# but a hunk is missing, and the builder would compile it. GNU patch leaves these
# behind when a later hunk of a file section fails after an earlier one applied,
# so clean up by rolling the whole series back and failing loudly.
leftovers="$(cd "$source_dir" && find . -name '*.rej' -o -name '*.orig' | head -n 20)"
if [[ -n "$leftovers" ]]; then
	echo "Patch application left rejected hunks behind:" >&2
	printf '%s\n' "$leftovers" >&2
	rollback
	(cd "$source_dir" && printf '%s\n' "$leftovers" | xargs -r rm -f --)
	exit 1
fi

echo "Applied ${#patches[@]} device backports to $source_commit"
if (( loosened > 0 )); then
	echo "Notice: $loosened of ${#patches[@]} patches needed bounded fuzz because the" \
		"source revision reworks the same board lists"
fi
