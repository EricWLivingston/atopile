"""PDF -> markdown via the LlamaParse REST API (the Python SDK is unusable on py3.14).

We talk to LlamaCloud directly with httpx: upload the file, poll the job, fetch the
markdown result. Same service / API key / ``parsing_instruction`` as the SDK, minus the
broken ``llama-index``/``llama-cloud`` dependency tree. Results are cached on disk by
source hash so re-parsing a known file is free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import warnings
from pathlib import Path

import httpx

from .config import LLAMA_PARSE_BASE_URL, PARSED_CACHE, DocType

logger = logging.getLogger(__name__)

DATASHEET_INSTRUCTION = """\
You are parsing an electronics datasheet. Output well-structured markdown.

Required behavior:
- Detect and preserve section headings (e.g. "Electrical Characteristics",
  "Absolute Maximum Ratings", "Recommended Operating Conditions",
  "Typical Application Circuit", "Pin Description", "Ordering Information").
  Render each as a ## heading.
- Tables MUST be preserved as markdown tables with all column headers intact.
  Do NOT flatten multi-row headers; merge them with a " / " separator.
- MERGED CELLS: when one cell spans multiple columns in the source (e.g. a single
  rating that applies to several part-number variants), REPLICATE that value into
  EVERY column it spans. Never leave a spanned column empty. Likewise replicate
  row-spanning cells into every row they cover.
- Never shift values between columns. If a source cell is genuinely empty, keep it
  empty; every table row must have exactly as many cells as the header.
- For "Electrical Characteristics" tables, keep the test conditions (Vin, Iout, Ta)
  with their row, not just in the table header.
- Footnotes referenced from table cells must be inlined as italicized notes after
  the table.
- Keep the manufacturer part number prominent (usually a first-page heading/banner).
- For schematics or pinout diagrams, describe as: "[Diagram: <description>]" — but
  ONLY for actual graphical figures. Never replace equations, formulas, or body text
  with a placeholder: transcribe equations (LaTeX) and keep every surrounding sentence,
  especially recommended component values.
"""

TEXTBOOK_INSTRUCTION = """\
You are parsing an electronics/engineering textbook. Output well-structured markdown.

Required behavior:
- Render chapter titles as ## headings and section/subsection titles as ### headings,
  keeping chapter/section numbers (e.g. "## Chapter 4: Diode Circuits",
  "### 4.2 Rectifier Topologies").
- Transcribe ALL equations and formulas in LaTeX. Never replace an equation, formula,
  or its surrounding prose with a placeholder; keep recommended component values and
  rules of thumb verbatim.
- Keep worked examples intact under their own heading: problem statement, solution
  steps, and result.
- Tables MUST be preserved as markdown tables with all column headers intact. Never
  shift values between columns; every row must have exactly as many cells as the header.
- For figures/schematics, describe as "[Figure <n>: <description>]" — ONLY for actual
  graphics, never for text or equations.
- Skip running page headers/footers and the index; inline footnotes as italicized
  notes where they are referenced.
"""

# app_notes reuse the datasheet instruction (lighter content, same structure rules).
_INSTRUCTIONS: dict[DocType, str] = {
    "datasheet": DATASHEET_INSTRUCTION,
    "app_note": DATASHEET_INSTRUCTION,
    "textbook": TEXTBOOK_INSTRUCTION,
}


# markdown table separator row, e.g. ``| ---- | :--: |``
_SEPARATOR_RE = re.compile(r"^\s*\|[\s\-:|]+\|?\s*$")
_BR_RE = re.compile(r"<br\s*/?>")


def _strip_header_title_pollution(md: str) -> str:
    """Remove a section title replicated into every table header cell.

    LlamaParse premium mode can emit headers like
    ``| <Section Title><br/>Min. | <Section Title><br/>Typ. | <Section Title> |``.
    When two or more header cells share the same ``<br/>``-delimited prefix, the
    prefix is the section title, not header content: strip it, and blank any header
    cell that *is* the bare prefix (e.g. a per-variant grouping column). Cache files
    keep the raw API output; this runs on every ``parse()`` read (idempotent).
    """
    lines = md.splitlines()
    for i in range(len(lines) - 1):
        line = lines[i]
        if not (line.lstrip().startswith("|") and _SEPARATOR_RE.match(lines[i + 1])):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        prefixes = [
            _BR_RE.split(c, maxsplit=1)[0].strip() for c in cells if _BR_RE.search(c)
        ]
        common = {p for p in prefixes if p and prefixes.count(p) >= 2}
        if not common:
            continue
        cleaned = []
        for c in cells:
            parts = _BR_RE.split(c, maxsplit=1)
            if len(parts) == 2 and parts[0].strip() in common:
                cleaned.append(parts[1].strip())
            elif c in common:
                cleaned.append("")
            else:
                cleaned.append(c)
        lines[i] = "| " + " | ".join(cleaned) + " |"
    return "\n".join(lines) + ("\n" if md.endswith("\n") else "")


def _cache_path(path: Path, doc_type: DocType) -> Path:
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return PARSED_CACHE / doc_type / f"{h}.md"


def _apply_sidecar_patch(md: str, cache: Path) -> str:
    """Apply manual corrections from ``<hash>.patch.json`` next to the cache file.

    Last resort for parse defects neither the instruction nor premium mode fixes
    (e.g. residual column shifts in individual table rows). Format: a JSON list of
    ``{"find": …, "replace": …, "note": …}``; each ``find`` must occur exactly once.
    A non-matching or ambiguous entry is skipped with a warning rather than applied
    blindly — after a ``--reparse`` the text may have changed and the patch must be
    re-validated, not trusted.
    """
    patch_file = cache.with_suffix(".patch.json")
    if not patch_file.exists():
        return md
    try:
        entries = json.loads(patch_file.read_text())
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"{patch_file.name}: not valid JSON ({exc}); ignored.", stacklevel=2
        )
        return md
    if not isinstance(entries, list):
        warnings.warn(
            f"{patch_file.name}: expected a JSON list of patch entries; ignored.",
            stacklevel=2,
        )
        return md
    applied = 0
    for entry in entries:
        # Validate the entry shape before touching the text — a malformed patch must be
        # skipped (with a warning), never raise or apply blindly (CODE_AUDIT Q3).
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("find"), str)
            or not isinstance(entry.get("replace"), str)
        ):
            warnings.warn(
                f"{patch_file.name}: skipped malformed entry "
                f"(need string 'find' and 'replace'): {entry!r:.80}",
                stacklevel=2,
            )
            continue
        find = entry["find"]
        if md.count(find) != 1:
            warnings.warn(
                f"{patch_file.name}: patch entry matched {md.count(find)}x "
                f"(expected 1), skipped: {entry.get('note', find[:60])}",
                stacklevel=2,
            )
            continue
        md = md.replace(find, entry["replace"])
        applied += 1
        logger.info(
            "%s: applied patch (%s)", patch_file.name, entry.get("note", find[:60])
        )
    if applied:
        logger.info("%s: applied %d/%d patches", patch_file.name, applied, len(entries))
    return md


def _api_key() -> str:
    key = os.environ.get("LLAMA_CLOUD_API_KEY")
    if not key:
        raise RuntimeError(
            "LLAMA_CLOUD_API_KEY is not set. Add it to your .env "
            "(get one at https://cloud.llamaindex.ai)."
        )
    return key


def parse(
    path: Path,
    doc_type: DocType,
    *,
    force: bool = False,
    premium: bool = False,
    poll_interval: float = 2.0,
    timeout_s: float = 300.0,
) -> str:
    """Parse ``path`` to markdown. Cached by source hash unless ``force``.

    ``premium`` (or env ``EE_PARSE_PREMIUM=1``) enables LlamaParse premium mode —
    much better at complex tables (merged cells, multi-variant pin tables) at a
    higher per-page credit cost. Use per-document when the table-fidelity scanner
    flags a doc that the standard mode + instruction can't fix.
    """
    cache = _cache_path(path, doc_type)
    if cache.exists() and not force:
        md = _apply_sidecar_patch(cache.read_text(), cache)
        return _strip_header_title_pollution(md)

    instruction = _INSTRUCTIONS.get(doc_type, DATASHEET_INSTRUCTION)
    headers = {"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"}

    data = {"parsing_instruction": instruction, "result_type": "markdown"}
    if premium or os.environ.get("EE_PARSE_PREMIUM") == "1":
        data["premium_mode"] = "true"

    with httpx.Client(
        base_url=LLAMA_PARSE_BASE_URL, headers=headers, timeout=60.0
    ) as c:
        # 1. upload
        files = {"file": (path.name, path.read_bytes(), "application/pdf")}
        resp = c.post(
            "/api/v1/parsing/upload",
            files=files,
            data=data,
        )
        resp.raise_for_status()
        job_id = resp.json()["id"]

        # 2. poll
        deadline = time.monotonic() + timeout_s
        while True:
            status = c.get(f"/api/v1/parsing/job/{job_id}").json().get("status")
            if status == "SUCCESS":
                break
            if status == "ERROR":
                raise RuntimeError(f"LlamaParse job {job_id} failed for {path.name}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"LlamaParse job {job_id} timed out ({timeout_s}s)")
            time.sleep(poll_interval)

        # 3. fetch the JSON result (per-page markdown) and synthesize deterministic
        # <!-- page N --> markers. Asking the parse instruction to emit markers
        # proved unreliable (LlamaParse ignored it); per-page assembly cannot miss.
        result = c.get(f"/api/v1/parsing/job/{job_id}/result/json").json()
        pages = result.get("pages") or []
        if pages:
            md = "\n".join(
                f"<!-- page {p.get('page', i + 1)} -->\n"
                f"{(p.get('md') or p.get('markdown') or '')}"
                for i, p in enumerate(pages)
            )
        else:  # fallback: plain markdown result, no page granularity
            md = (
                c.get(f"/api/v1/parsing/job/{job_id}/result/markdown")
                .json()
                .get("markdown", "")
            )

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(md)
    return _strip_header_title_pollution(_apply_sidecar_patch(md, cache))
