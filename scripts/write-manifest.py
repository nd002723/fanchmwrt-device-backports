#!/usr/bin/env python3
"""Record one release baseline in ``manifest.json``.

The manifest is the contract between this repository and the builder: it names
the FanchmWrt release the patches were written against (``base_commit``), the
builder-side ref that carries the private layer (``branch``), and how many patch
files the set contains. ``scripts/apply.sh`` refuses to run when the count does
not match, so the count is recomputed here instead of being copied by hand.

    scripts/write-manifest.py --ref v25.12.7 --commit <sha> --patches patches
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_MANIFEST = Path("manifest.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", required=True, help="upstream release tag, for example v25.12.7")
    parser.add_argument("--commit", required=True, help="commit the tag resolves to")
    parser.add_argument(
        "--branch",
        help="builder-side maintenance branch (default: fanchmwrt-<version>)",
    )
    parser.add_argument("--patches", default="patches", help="directory holding the patch files")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest to rewrite")
    args = parser.parse_args()

    version = args.ref[1:] if args.ref.startswith("v") else args.ref
    branch = args.branch or f"fanchmwrt-{version}"

    path = Path(args.manifest)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot read {path}: {error}")

    patch_dir = Path(args.patches)
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
