"""Table-fidelity scanner: detect parse defects that misattribute spec values.

LlamaParse flattens **merged cells**: a value that spans several part-number columns in
the source PDF (e.g. ``VRRM = 40 V`` covering two diode variants) lands in only one
column of the markdown table, leaving the sibling columns empty — so a model reading the
chunk can misattribute the spec to the wrong part *with a confident citation*. A related
defect is **column drift**: ragged rows whose values shift across columns (seen in
multi-variant pin tables). A third flavor is **column shift**: a header column whose
body cells split/merge per row pushes every later value one column left (seen in the
CD0603 Electrical Characteristics table — Typ values landed under Min, Max came out
empty), which cell counts alone cannot catch.

This scanner cannot prove a cell *should* span (the markdown has no span info), so it
reports *suspects* for human/agent review and acts as a free regression gate after any
re-parse: counts must not grow, and known-fixed tables must scan clean.

Usage:
    uv run python -m ee_agent_rag.eval.table_fidelity            # scan the parse cache
    uv run python -m ee_agent_rag.eval.table_fidelity --strict   # exit 1 on any finding
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import PARSED_CACHE

# A header cell naming a concrete part variant, e.g. "CD0603-B0240R", "Pin NVT2008BQ".
_PART_COL_RE = re.compile(r"[A-Z]{1,4}\d{3,}")

# Column-shift heuristic thresholds (EC-style Min/Typ/Max tables).
_SHIFT_MIN_ROWS = 4
_SHIFT_MIN_FILL = 0.8


@dataclass
class Finding:
    kind: str  # "span_suspect" | "ragged_row" | "column_shift_suspect"
    table_header: str
    row: str
    detail: str


def _header_label(cell: str) -> str:
    """Normalized header label: text after the last ``<br/>`` (raw cached premium
    parses replicate the section title into every header cell), lowercased, with
    trailing punctuation dropped — so ``…<br/>Min.`` matches ``min``."""
    tail = re.split(r"<br\s*/?>", cell)[-1]
    return tail.strip().rstrip(".:").lower()


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(md: str) -> list[list[str]]:
    """Contiguous ``|``-prefixed line blocks with at least header+separator+1 row."""
    tables, block = [], []
    for line in md.splitlines() + [""]:
        if line.lstrip().startswith("|"):
            block.append(line)
        else:
            if len(block) >= 3:
                tables.append(block)
            block = []
    return tables


def scan_markdown(md: str) -> list[Finding]:
    """Scan one parsed document's markdown for table-fidelity suspects."""
    findings: list[Finding] = []
    for tbl in _tables(md):
        header = _split_row(tbl[0])
        n_cols = len(header)
        part_cols = [
            j
            for j, h in enumerate(header)
            if _PART_COL_RE.search(h) and len(h) >= 6
        ]
        labels = [_header_label(h) for h in header]
        min_idx = next(
            (j for j, x in enumerate(labels) if x in ("min", "minimum")), None
        )
        max_idx = next(
            (j for j, x in enumerate(labels) if x in ("max", "maximum")), None
        )
        shift_rows = shift_min_filled = shift_max_filled = 0
        for row_line in tbl[2:]:
            cells = _split_row(row_line)
            if len(cells) != n_cols:
                findings.append(
                    Finding(
                        kind="ragged_row",
                        table_header=tbl[0].strip(),
                        row=row_line.strip(),
                        detail=f"{len(cells)} cells vs {n_cols} header columns",
                    )
                )
                continue
            if min_idx is not None and max_idx is not None:
                shift_rows += 1
                shift_min_filled += bool(cells[min_idx])
                shift_max_filled += bool(cells[max_idx])
            if len(part_cols) >= 2:
                vals = [cells[j] for j in part_cols]
                filled = [v for v in vals if v]
                if filled and len(filled) < len(vals):
                    findings.append(
                        Finding(
                            kind="span_suspect",
                            table_header=tbl[0].strip(),
                            row=row_line.strip(),
                            detail=(
                                f"{len(filled)}/{len(vals)} part columns filled — "
                                "possible flattened merged cell"
                            ),
                        )
                    )
        if (
            shift_rows >= _SHIFT_MIN_ROWS
            and shift_min_filled / shift_rows >= _SHIFT_MIN_FILL
            and shift_max_filled == 0
        ):
            findings.append(
                Finding(
                    kind="column_shift_suspect",
                    table_header=tbl[0].strip(),
                    row=tbl[2].strip(),
                    detail=(
                        f"Min {shift_min_filled}/{shift_rows} filled but Max empty "
                        "over the whole table — values likely shifted one column left"
                    ),
                )
            )
    return findings


def scan_cache(cache_dir: Path | None = None) -> dict[str, list[Finding]]:
    """Scan every parsed markdown in the cache; returns ``{file_name: findings}``."""
    cache_dir = cache_dir or PARSED_CACHE
    out: dict[str, list[Finding]] = {}
    for md_file in sorted(cache_dir.rglob("*.md")):
        findings = scan_markdown(md_file.read_text())
        if findings:
            out[md_file.name] = findings
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=None)
    ap.add_argument(
        "--strict", action="store_true", help="exit non-zero on any finding"
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="print each row")
    args = ap.parse_args()

    report = scan_cache(args.cache_dir)
    total = 0
    for fname, findings in report.items():
        spans = sum(1 for f in findings if f.kind == "span_suspect")
        ragged = sum(1 for f in findings if f.kind == "ragged_row")
        shifts = sum(1 for f in findings if f.kind == "column_shift_suspect")
        total += len(findings)
        print(
            f"{fname}: {spans} span suspects, {ragged} ragged rows, "
            f"{shifts} column-shift suspects"
        )
        if args.verbose:
            for f in findings:
                print(f"  [{f.kind}] {f.row[:110]}")
                print(f"           {f.detail}")
    if not report:
        print("clean: no table-fidelity suspects found")
    if args.strict and total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
