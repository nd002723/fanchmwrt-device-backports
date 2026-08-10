# FanchmWrt Device Backports

Device support missing from FanchmWrt but available in OpenWrt is kept here as
an optional patch set. FanchmWrt remains the primary source: a builder should
use a native FanchmWrt profile without downloading or applying this repository.

The patch set is only valid for the exact FanchmWrt base recorded in
`manifest.json`. Run:

```sh
bash ./scripts/apply.sh /path/to/fanchmwrt
```

The command refuses unsupported source revisions and rolls back patches already
applied during the current run if a later patch fails.
