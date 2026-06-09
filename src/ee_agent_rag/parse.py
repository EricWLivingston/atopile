"""PDF -> markdown via the LlamaParse REST API (the Python SDK is unusable on py3.14).

We talk to LlamaCloud directly with httpx: upload the file, poll the job, fetch the
markdown result. Same service / API key / ``parsing_instruction`` as the SDK, minus the
broken ``llama-index``/``llama-cloud`` dependency tree. Results are cached on disk by
source hash so re-parsing a known file is free.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import httpx

from .config import LLAMA_PARSE_BASE_URL, PARSED_CACHE, DocType

DATASHEET_INSTRUCTION = """\
You are parsing an electronics datasheet. Output well-structured markdown.

Required behavior:
- Detect and preserve section headings (e.g. "Electrical Characteristics",
  "Absolute Maximum Ratings", "Recommended Operating Conditions",
  "Typical Application Circuit", "Pin Description", "Ordering Information").
  Render each as a ## heading.
- Tables MUST be preserved as markdown tables with all column headers intact.
  Do NOT flatten multi-row headers; merge them with a " / " separator.
- For "Electrical Characteristics" tables, keep the test conditions (Vin, Iout, Ta)
  with their row, not just in the table header.
- Footnotes referenced from table cells must be inlined as italicized notes after
  the table.
- Keep the manufacturer part number prominent (usually a first-page heading/banner).
- For schematics or pinout diagrams, describe as: "[Diagram: <description>]".
- Insert a page marker as an HTML comment at each page boundary: <!-- page N -->
"""

# app_notes reuse the datasheet instruction (lighter content, same structure rules).
_INSTRUCTIONS: dict[DocType, str] = {
    "datasheet": DATASHEET_INSTRUCTION,
    "app_note": DATASHEET_INSTRUCTION,
}


def _cache_path(path: Path, doc_type: DocType) -> Path:
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return PARSED_CACHE / doc_type / f"{h}.md"


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
    poll_interval: float = 2.0,
    timeout_s: float = 300.0,
) -> str:
    """Parse ``path`` to markdown. Cached by source hash unless ``force``."""
    cache = _cache_path(path, doc_type)
    if cache.exists() and not force:
        return cache.read_text()

    instruction = _INSTRUCTIONS.get(doc_type, DATASHEET_INSTRUCTION)
    headers = {"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"}

    with httpx.Client(
        base_url=LLAMA_PARSE_BASE_URL, headers=headers, timeout=60.0
    ) as c:
        # 1. upload
        files = {"file": (path.name, path.read_bytes(), "application/pdf")}
        resp = c.post(
            "/api/v1/parsing/upload",
            files=files,
            data={"parsing_instruction": instruction, "result_type": "markdown"},
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

        # 3. fetch markdown
        result = c.get(f"/api/v1/parsing/job/{job_id}/result/markdown").json()
        md = result.get("markdown", "")

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(md)
    return md
