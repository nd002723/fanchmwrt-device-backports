#!/usr/bin/env python3
"""Record one release baseline in ``manifest.json``.

The manifest is the contract between this repository and the builder: it names
the FanchmWrt release the patches were written against (``base_commit``), the
builder-side ref that carries the private layer (``branch``), and how many patch
files the set contains. ``scripts/apply.sh`` refuses to run when the count does
not match, so the count is recomputed here instead of being copied by hand.

    scripts/write-manifest.py --ref v25.12.7 --commit <sha> --patches patches

``--check`` validates the recorded file without writing it, so a follow run fails
loudly when a hand edit leaves the manifest contradicting itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_MANIFEST = Path("manifest.json")

# The builder only ever sends one of these three values in its `architecture`
# field (see its issue form and validate-openwrt-target.sh, where `arm` covers
# both arm and aarch64 target tuples). Listing a value the builder never sends
# would describe this patch set in a vocabulary no consumer reads -- which is how
# `aarch64` ended up here.
BUILDER_ARCHITECTURES = ("arm", "mipsel", "x86")


def validate(manifest: dict, patch_dir: Path) -> list[str]:
    """Return every way the recorded manifest contradicts itself."""
    problems: list[str] = []

    if manifest.get("schema_version") != 1:
        problems.append(f"schema_version must be 1, found {manifest.get('schema_version')!r}")

    fanchmwrt = manifest.get("fanchmwrt")
    if not isinstance(fanchmwrt, dict):
        problems.append("fanchmwrt must be an object naming base_commit, ref and branch")
        fanchmwrt = {}
    base = str(fanchmwrt.get("base_commit", ""))
    if len(base) != 40 or any(ch not in "0123456789abcdef" for ch in base):
        problems.append(f"fanchmwrt.base_commit must be a 40-character lowercase SHA, found {base!r}")
    for key in ("ref", "branch"):
        if not str(fanchmwrt.get(key, "")).strip():
            problems.append(f"fanchmwrt.{key} must name a release tag or branch")

    architectures = manifest.get("architectures")
    if not isinstance(architectures, list) or not architectures:
        problems.append("architectures must be a non-empty list")
    else:
        unknown = [value for value in architectures if value not in BUILDER_ARCHITECTURES]
        if unknown:
            problems.append(
                "architectures must use the builder's vocabulary "
                f"{list(BUILDER_ARCHITECTURES)}, found {unknown} (map aarch64 to arm)"
            )
        duplicates = sorted({value for value in architectures if architectures.count(value) > 1})
        if duplicates:
            problems.append(f"architectures lists {duplicates} more than once")

    count = len(sorted(patch_dir.glob("*.patch")))
    if manifest.get("patch_count") != count:
        problems.append(f"patch_count is {manifest.get('patch_count')!r} but {patch_dir} holds {count}")

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", help="upstream release tag, for example v25.12.7")
    parser.add_argument("--commit", help="commit the tag resolves to")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the recorded manifest instead of writing it",
    )
    parser.add_argument(
        "--branch",
        help="builder-side maintenance branch (default: fanchmwrt-<version>)",
    )
    parser.add_argument("--patches", default="patches", help="directory holding the patch files")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest to rewrite")
    args = parser.parse_args()

    path = Path(args.manifest)
    patch_dir = Path(args.patches)

    if not args.check and not (args.ref and args.commit):
        parser.error("--ref and --commit are required unless --check is given")

    if args.check:
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"cannot read {path}: {error}")
        problems = validate(recorded, patch_dir)
        if problems:
            for problem in problems:
                print(f"FAIL {problem}", file=sys.stderr)
            raise SystemExit(1)
        print(f"{path}: {recorded['fanchmwrt']['ref']} @ {recorded['fanchmwrt']['base_commit'][:12]}, "
              f"{recorded['patch_count']} patches, {recorded.get('profile_count', '?')} profiles, "
              f"architectures {recorded['architectures']}")
        return

    version = args.ref[1:] if args.ref.startswith("v") else args.ref
    branch = args.branch or f"fanchmwrt-{version}"

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot read {path}: {error}")

    count = len(sorted(patch_dir.glob("*.patch")))
    if count == 0:
        raise SystemExit(f"no patch files under {patch_dir}")

    manifest.setdefault("fanchmwrt", {})
    manifest["fanchmwrt"].update(
        repository=manifest["fanchmwrt"].get("repository", "fanchmwrt/fanchmwrt"),
        base_commit=args.commit,
        ref=args.ref,
        branch=branch,
    )
    manifest["patch_count"] = count
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path}: {args.ref} @ {args.commit[:12]}, {count} patches, branch {branch}")


if __name__ == "__main__":
    main()
