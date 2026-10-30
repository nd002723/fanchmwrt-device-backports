#!/usr/bin/env python3
"""Try to re-anchor a patch hunk by hunk so it applies to a drifted tree.

A device backport patch pins its insertion point with surrounding context
lines. When upstream edits that neighbourhood, ``git apply`` rejects the patch
even though the change itself is still meaningful. Three-way merging does not
help: ``git apply --3way`` reports the same conflicts, because "upstream deleted
this line" and "the patch inserts next to it" overlap.

This tool does one narrow, reversible thing: for every hunk it re-reads the
context from the target file and rebuilds the hunk around the unchanged
added/removed lines. Context lines that no longer exist upstream are dropped,
lines that were reworded are taken from the target verbatim, and the hunk
header counts are recomputed. Added and removed lines are never rewritten,
reordered, or dropped -- if a hunk cannot be re-anchored that way, the patch is
left untouched.

The result is a candidate, not a decision: it must still be validated by
applying the whole set and enumerating devices.
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class RepairError(Exception):
    pass


@dataclass
class Hunk:
    header_suffix: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    body: list[str] = field(default_factory=list)

    def render(self) -> list[str]:
        return [
            f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@{self.header_suffix}",
            *self.body,
        ]


@dataclass
class FilePatch:
    header: list[str]
    path: str
    hunks: list[Hunk]
    creates_file: bool = False
    deletes_file: bool = False

    def render(self) -> list[str]:
        lines = list(self.header)
        for hunk in self.hunks:
            lines.extend(hunk.render())
        return lines


def split_patch(text: str) -> tuple[list[str], list[FilePatch]]:
    lines = text.splitlines()
    preamble: list[str] = []
    files: list[FilePatch] = []
    index = 0

    while index < len(lines) and not lines[index].startswith("diff --git "):
        preamble.append(lines[index])
        index += 1

    while index < len(lines):
        header: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@ "):
            header.append(lines[index])
            index += 1
        if not header:
            break

        path = ""
        creates_file = False
        deletes_file = False
        for line in header:
            if line.startswith("--- /dev/null"):
                creates_file = True
            if line.startswith("+++ /dev/null"):
                deletes_file = True
            if line.startswith("+++ b/"):
                path = line[6:]
                break
            if line.startswith("--- a/"):
                path = line[6:]
        if not path:
            raise RepairError("cannot determine the target path of a file section")

        hunks: list[Hunk] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            match = HUNK_HEADER_RE.match(lines[index])
            if match is None:
                raise RepairError(f"unparsable hunk header: {lines[index]!r}")
            hunk = Hunk(
                header_suffix=match.group(5),
                old_start=int(match.group(1)),
                old_len=int(match.group(2)) if match.group(2) is not None else 1,
                new_start=int(match.group(3)),
                new_len=int(match.group(4)) if match.group(4) is not None else 1,
            )
            index += 1
            while index < len(lines) and not lines[index].startswith("@@ ") and not lines[index].startswith("diff --git "):
                hunk.body.append(lines[index])
                index += 1
            hunks.append(hunk)
        files.append(FilePatch(header=header, path=path, hunks=hunks, creates_file=creates_file, deletes_file=deletes_file))

    return preamble, files


def change_span(body: list[str]) -> tuple[int, int] | None:
    """Return [first, last] indexes of added/removed lines, or None."""
    positions = [i for i, line in enumerate(body) if line[:1] in {"+", "-"}]
    if not positions:
        return None
    return positions[0], positions[-1]


def is_single_block(body: list[str]) -> bool:
    span = change_span(body)
    if span is None:
        return False
    first, last = span
    return all(line[:1] in {"+", "-"} for line in body[first : last + 1])


def old_side_lines(body: list[str]) -> list[str]:
    """Lines the hunk expects to find in the file: context plus removals."""
    return [line[1:] for line in body if line[:1] in {" ", "-"}]


def new_side_lines(body: list[str]) -> list[str]:
    return [line[1:] for line in body if line[:1] in {" ", "+"}]


def similar(left: str, right: str, threshold: float = 0.8) -> bool:
    """True when ``right`` is a plausible replacement for ``left``.

    Upstream usually rewords a line slightly (``h3c_magic-nx30-pro-stock``
    becomes ``h3c_magic-nx30-pro``), so an exact comparison throws away the one
    anchor that still points at the right place. The cheap prefix test keeps the
    window scan affordable on multi-thousand-line files.
    """
    if left == right:
        return True
    if not left.strip() and not right.strip():
        return True
    if len(left) < 4 or len(right) < 4 or left[:4] != right[:4]:
        return False
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio() >= threshold


def locate(target: list[str], pattern: list[str]) -> tuple[int, float]:
    """Best window of ``target`` for ``pattern``; returns (start, score)."""
    if not pattern or not target:
        return -1, 0.0
    size = len(pattern)
    if size > len(target):
        return -1, 0.0

    best_start, best_hits = -1, -1
    for start in range(len(target) - size + 1):
        hits = sum(1 for offset in range(size) if similar(pattern[offset], target[start + offset]))
        if hits > best_hits:
            best_start, best_hits = start, hits
            if hits == size:
                break
    return best_start, (best_hits / size if best_hits >= 0 else 0.0)


def align(pattern: list[str], window: list[str]) -> list[str | None]:
    """Map old-side lines onto the target window, allowing reworded lines."""
    rows, columns = len(pattern), len(window)
    table = [[0] * (columns + 1) for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for column in range(columns - 1, -1, -1):
            if similar(pattern[row], window[column]):
                table[row][column] = table[row + 1][column + 1] + 1
            else:
                table[row][column] = max(table[row + 1][column], table[row][column + 1])

    mapped: list[str | None] = [None] * rows
    row = column = 0
    while row < rows and column < columns:
        if similar(pattern[row], window[column]) and table[row][column] == table[row + 1][column + 1] + 1:
            mapped[row] = window[column]
            row += 1
            column += 1
        elif table[row + 1][column] >= table[row][column + 1]:
            row += 1
        else:
            column += 1
    return mapped


def relax_hunk(hunk: Hunk, target: list[str]) -> tuple[list[str], list[str]] | None:
    """Rebuild ``hunk`` around the unchanged changes; returns (body, notes)."""
    if not is_single_block(hunk.body):
        return None
    span = change_span(hunk.body)
    assert span is not None
    first, last = span

    before = hunk.body[:first]
    changes = hunk.body[first : last + 1]
    after = hunk.body[last + 1 :]

    pattern = [line[1:] for line in before if line[:1] == " "] + [line[1:] for line in changes if line[:1] == "-"] + [
        line[1:] for line in after if line[:1] == " "
    ]
    if not pattern:
        return None

    start, score = locate(target, pattern)
    if start < 0 or score < 0.5:
        return None

    window = target[start : start + len(pattern)]
    mapped = align(pattern, window)
    notes: list[str] = []

    def rebuild(lines: list[str], offset: int, keep_blank: bool) -> tuple[list[str], int]:
        rebuilt: list[str] = []
        consumed = 0
        for position, line in enumerate(lines):
            if line[:1] != " ":
                rebuilt.append(line)
                continue
            replacement = mapped[offset + position]
            if replacement is None:
                if keep_blank and not line[1:].strip():
                    # A blank separator after the insertion vanished upstream.
                    # Add one instead of gluing the new block to its neighbour.
                    notes.append("added a blank line to keep the separation")
                    rebuilt.append("+")
                    continue
                notes.append(f"dropped context {line[1:]!r}")
                continue
            if replacement != line[1:]:
                notes.append(f"re-anchored context {line[1:]!r} -> {replacement!r}")
            rebuilt.append(" " + replacement)
            consumed += 1
        return rebuilt, consumed

    new_before, consumed_before = rebuild(before, 0, keep_blank=False)
    removed = sum(1 for line in changes if line[:1] == "-")
    new_after, consumed_after = rebuild(after, len(before) + removed, keep_blank=True)

    if not notes:
        return None
    if not any(line[:1] == " " and line[1:].strip() for line in body_anchor(new_before, new_after)):
        return None

    body = new_before + changes + new_after
    return body, notes


def body_anchor(before: list[str], after: list[str]) -> list[str]:
    """Context lines kept after re-anchoring; a hunk with none has no anchor."""
    return [line for line in before + after if line[:1] == " "]


def rebuild_hunk(hunk: Hunk, body: list[str]) -> Hunk:
    old_len = sum(1 for line in body if line[:1] in {" ", "-"})
    new_len = sum(1 for line in body if line[:1] in {" ", "+"})
    return Hunk(
        header_suffix=hunk.header_suffix,
        old_start=hunk.old_start,
        old_len=old_len,
        new_start=hunk.new_start,
        new_len=new_len,
        body=body,
    )


def read_target(source: Path, path: str) -> list[str]:
    candidate = source / path
    if not candidate.is_file():
        raise RepairError(f"target file is missing: {path}")
    return candidate.read_text(encoding="utf-8", errors="surrogateescape").splitlines()


def git_apply_check(source: Path, patch_text: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "-C", str(source), "apply", "--check", "--whitespace=nowarn", "-"],
        input=patch_text,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0, (result.stderr or "").strip()


def repair(patch_text: str, source: Path) -> tuple[str, list[str]]:
    preamble, files = split_patch(patch_text)
    notes: list[str] = []

    for file_patch in files:
        if file_patch.creates_file or file_patch.deletes_file:
            continue
        try:
            target = read_target(source, file_patch.path)
        except RepairError as error:
            notes.append(f"{file_patch.path}: skipped ({error})")
            continue
        rebuilt: list[Hunk] = []
        for number, hunk in enumerate(file_patch.hunks, start=1):
            result = relax_hunk(hunk, target)
            if result is None:
                rebuilt.append(hunk)
                continue
            body, hunk_notes = result
            rebuilt.append(rebuild_hunk(hunk, body))
            for note in hunk_notes:
                notes.append(f"{file_patch.path} hunk {number}: {note}")
        file_patch.hunks = rebuilt

    lines = list(preamble)
    for file_patch in files:
        lines.extend(file_patch.render())
    return "\n".join(lines) + ("\n" if patch_text.endswith("\n") else ""), notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patch", required=True, help="Patch file to re-anchor")
    parser.add_argument("--source", required=True, help="Source tree the patch must apply to")
    parser.add_argument("--output", help="Where to write the repaired patch (default: overwrite in place)")
    parser.add_argument("--report", help="Optional file receiving one note per re-anchored line")
    args = parser.parse_args()

    patch_path = Path(args.patch)
    source = Path(args.source)
    original = patch_path.read_text(encoding="utf-8", errors="surrogateescape")

    applies, _ = git_apply_check(source, original)
    if applies:
        if args.output:
            Path(args.output).write_text(original, encoding="utf-8", errors="surrogateescape")
        print(f"{patch_path.name}: applies as-is, nothing to repair")
        return

    try:
        repaired, notes = repair(original, source)
    except RepairError as error:
        print(f"{patch_path.name}: cannot repair: {error}", file=sys.stderr)
        raise SystemExit(1)

    if repaired == original:
        print(f"{patch_path.name}: no re-anchorable hunk found", file=sys.stderr)
        raise SystemExit(1)

    applies_after, stderr = git_apply_check(source, repaired)
    if not applies_after:
        print(f"{patch_path.name}: re-anchored patch still does not apply: {stderr}", file=sys.stderr)
        raise SystemExit(1)

    destination = Path(args.output) if args.output else patch_path
    destination.write_text(repaired, encoding="utf-8", errors="surrogateescape")
    if args.report:
        Path(args.report).write_text("\n".join(notes) + "\n", encoding="utf-8")
    print(f"{patch_path.name}: re-anchored ({len(notes)} line(s))")
    for note in notes:
        print(f"  {note}")


if __name__ == "__main__":
    main()
