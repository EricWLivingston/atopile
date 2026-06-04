# RAG Ingestion Pipeline — Starter Code Skeleton

> Concrete module-by-module starter code. Each module is short on purpose — these are the seams to build against, not finished implementations.

---

## `rag/config.py`

```python
"""Centralized configuration for the RAG pipeline."""
import os
from pathlib import Path
from typing import Literal

DocType = Literal["datasheet", "app_note", "standard", "textbook", "internal"]
Corpus = Literal["datasheets", "app_notes", "standards", "textbooks", "internal_standards"]

DOC_TYPE_TO_CORPUS: dict[DocType, Corpus] = {
    "datasheet": "datasheets",
    "app_note": "app_notes",
    "standard": "standards",
    "textbook": "textbooks",
    "internal": "internal_standards",
}

# Embedding model
EMBED_MODEL = os.environ.get("EE_EMBED_MODEL", "voyage-3")
EMBED_DIM = 1024  # voyage-3
EMBED_BATCH_SIZE = 128

# Qdrant
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

# Chunking caps
DATASHEET_MAX_TOKENS = 3000
TEXTBOOK_TARGET_TOKENS = 600
TEXTBOOK_OVERLAP_TOKENS = 100
INTERNAL_MAX_TOKENS = 1500

# Storage roots
DATA_ROOT = Path(os.environ.get("EE_DATA_ROOT", "./data"))
PARSED_CACHE = DATA_ROOT / ".parsed_cache"  # parsed markdown lives here

# Recall@5 baselines (eval gate)
RECALL_BASELINES: dict[Corpus, float] = {
    "datasheets": 0.80,
    "standards": 0.90,
    "app_notes": 0.70,
    "textbooks": 0.65,
    "internal_standards": 0.85,
}
```

---

## `rag/classify.py`

```python
"""Identify document type from filename + first-page text."""
import re
from pathlib import Path
from .config import DocType

DATASHEET_SIGNALS = [
    r"\bdata\s?sheet\b",
    r"\bDS\d{4,}\b",
    r"\bElectrical Characteristics\b",
    r"\b(VCC|VDD|VIN|VOUT)\b.*\b(min|typ|max)\b",
]
STANDARD_SIGNALS = [
    r"\bIPC[-\s]?\d{4}",
    r"\bMIL[-\s]?STD[-\s]?\d+",
    r"\bJESD\d+",
    r"\bIEC\s\d{4,}",
    r"\bISO[/\s]+IEC\s\d{4,}",
]
APP_NOTE_SIGNALS = [
    r"\bapplication note\b",
    r"\bAN[-\s]?\d{2,5}\b",
    r"\bSLVA\d+",  # TI
    r"\bAN\d+\b.*manufacturer",
]

def _read_first_page_text(path: Path, max_chars: int = 8000) -> str:
    """Cheap text extraction using pdfminer.six (no LLM)."""
    from pdfminer.high_level import extract_text
    try:
        return extract_text(str(path), maxpages=1)[:max_chars]
    except Exception:
        return ""

def classify(path: Path) -> DocType:
    name = path.stem.lower()

    # 1. Path-based hints
    parts_lower = [p.lower() for p in path.parts]
    if "textbooks" in parts_lower:
        return "textbook"
    if "internal" in parts_lower:
        return "internal"

    # 2. Filename pattern match
    for pattern in STANDARD_SIGNALS:
        if re.search(pattern, name, re.I):
            return "standard"
    for pattern in DATASHEET_SIGNALS:
        if re.search(pattern, name, re.I):
            return "datasheet"

    # 3. First-page text scan (only for PDFs)
    if path.suffix.lower() == ".pdf":
        first_page = _read_first_page_text(path)
        for pattern in STANDARD_SIGNALS:
            if re.search(pattern, first_page, re.I):
                return "standard"
        for pattern in DATASHEET_SIGNALS:
            if re.search(pattern, first_page, re.I):
                return "datasheet"
        for pattern in APP_NOTE_SIGNALS:
            if re.search(pattern, first_page, re.I):
                return "app_note"

    # 4. Default: internal (markdown / docx)
    if path.suffix.lower() in {".md", ".docx"}:
        return "internal"

    # 5. Last resort: LLM (not implemented in skeleton)
    raise NotImplementedError(
        f"Could not classify {path}. Add LLM fallback or rename the file."
    )
```

---

## `rag/parse/datasheet_parser.py`

```python
"""Datasheet parser with electronics-tuned instruction."""
import os
import hashlib
from pathlib import Path
from llama_parse import LlamaParse
from ..config import PARSED_CACHE

DATASHEET_INSTRUCTION = """\
You are parsing an electronics datasheet. Output well-structured markdown.

Required behavior:
- Detect and preserve section headings (e.g. "Electrical Characteristics",
  "Absolute Maximum Ratings", "Typical Application Circuit", "Pin Description",
  "Ordering Information"). Render as ## headings.
- Tables MUST be preserved as markdown tables with all column headers intact.
  Do NOT flatten multi-row headers; merge them with " / " separator.
- For "Electrical Characteristics" tables, preserve test conditions
  (Vin, Iout, Ta) — they belong with the row, not just the table header.
- Footnotes referenced from table cells must be inlined as italicized notes
  after the table.
- Preserve the manufacturer part number prominently — usually on the first
  page in a heading or banner.
- For schematics or pinout diagrams, describe as: "[Diagram: <description>]".
- Insert page markers as HTML comments at each page boundary: <!-- page N -->
"""

def _cache_key(path: Path) -> Path:
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return PARSED_CACHE / "datasheets" / f"{h}.md"

def parse_datasheet(path: Path) -> str:
    """Parse a datasheet PDF to markdown. Cached by source hash."""
    cache_path = _cache_key(path)
    if cache_path.exists():
        return cache_path.read_text()

    parser = LlamaParse(
        api_key=os.environ["LLAMA_CLOUD_API_KEY"],
        result_type="markdown",
        parsing_instruction=DATASHEET_INSTRUCTION,
    )
    docs = parser.load_data(str(path))
    md = "\n\n".join(d.text for d in docs)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(md)
    return md
```

(`standard_parser.py`, `textbook_parser.py`, `markdown_parser.py` follow the same shape with their respective instructions.)

---

## `rag/parse/__init__.py`

```python
"""Dispatch parsing by document type."""
from pathlib import Path
from ..config import DocType
from .datasheet_parser import parse_datasheet
from .standard_parser import parse_standard
from .textbook_parser import parse_textbook
from .markdown_parser import parse_markdown

PARSERS = {
    "datasheet": parse_datasheet,
    "app_note": parse_datasheet,    # same parser, lighter prompt OK
    "standard": parse_standard,
    "textbook": parse_textbook,
    "internal": parse_markdown,
}

def parse(path: Path, doc_type: DocType) -> str:
    return PARSERS[doc_type](path)
```

---

## `rag/chunk/datasheet_chunker.py`

```python
"""Section-aware datasheet chunker."""
import re
from dataclasses import dataclass, field
from typing import Optional
from ..config import DATASHEET_MAX_TOKENS

@dataclass
class RawChunk:
    content: str
    section_path: str
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    extras: dict = field(default_factory=dict)

H2_SPLIT = re.compile(r"^##\s+(.+?)$", re.M)
H3_SPLIT = re.compile(r"^###\s+(.+?)$", re.M)
PAGE_MARKER = re.compile(r"<!--\s*page\s+(\d+)\s*-->")

def _approx_tokens(text: str) -> int:
    return len(text) // 4

def _find_page_range(content: str) -> tuple[Optional[int], Optional[int]]:
    pages = [int(m.group(1)) for m in PAGE_MARKER.finditer(content)]
    if not pages:
        return None, None
    return min(pages), max(pages)

def _split_at_headings(md: str, splitter: re.Pattern) -> list[tuple[str, str]]:
    """Returns [(heading, content_starting_with_heading), ...]."""
    matches = list(splitter.finditer(md))
    if not matches:
        return [("", md)]
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        heading = m.group(1).strip()
        content = md[start:end]
        out.append((heading, content))
    return out

def chunk_datasheet(parsed_md: str) -> list[RawChunk]:
    chunks: list[RawChunk] = []
    for section_heading, section_md in _split_at_headings(parsed_md, H2_SPLIT):
        # Skip the pre-first-section prelude unless it has meaningful content
        if not section_heading and _approx_tokens(section_md) < 50:
            continue

        if _approx_tokens(section_md) <= DATASHEET_MAX_TOKENS:
            page_start, page_end = _find_page_range(section_md)
            chunks.append(RawChunk(
                content=section_md,
                section_path=section_heading or "(preamble)",
                page_start=page_start,
                page_end=page_end,
            ))
        else:
            # Split into H3 sub-sections
            for sub_heading, sub_md in _split_at_headings(section_md, H3_SPLIT):
                page_start, page_end = _find_page_range(sub_md)
                chunks.append(RawChunk(
                    content=sub_md,
                    section_path=f"{section_heading} > {sub_heading}" if sub_heading else section_heading,
                    page_start=page_start,
                    page_end=page_end,
                ))
    return chunks
```

(`standard_chunker.py`, `textbook_chunker.py`, `markdown_chunker.py` follow.)

---

## `rag/chunk/ato_chunker.py`

```python
"""One chunk per atopile module block. For atopile_examples corpus."""
import re
from pathlib import Path
from rag.config import InternalChunk

MODULE_RE = re.compile(
    r"^module\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:?(?P<body>(?:\n(?:    .*|\n))+)",
    re.MULTILINE,
)
IMPORT_RE = re.compile(r"^(?:import\s+\S+|from\s+\".+?\"\s+import\s+\S+)\s*$", re.MULTILINE)
PRAGMA_RE = re.compile(r"^#pragma\s+.*$", re.MULTILINE)

# Pre-cached on startup from atopile's stdlib_list tool — see 01_ORCHESTRATOR.md.
STDLIB_TYPES = {
    "ElectricPower", "ElectricLogic", "Electrical",
    "I2C", "SPI", "CAN", "USB2_0", "USB3_0", "UART",
    "Resistor", "Capacitor", "Inductor", "Diode", "LED", "Fuse",
    # ... full 102-name list
}


def chunk_ato_file(path: Path, project: str) -> list[InternalChunk]:
    content = path.read_text()
    imports = "\n".join(IMPORT_RE.findall(content))
    pragmas = "\n".join(PRAGMA_RE.findall(content))

    chunks = []
    for match in MODULE_RE.finditer(content):
        name = match.group("name")
        body = match.group(0)
        chunk_text = f"{pragmas}\n\n{imports}\n\n{body}".strip()
        stdlib_used = sorted({
            tok for tok in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", body)
            if tok in STDLIB_TYPES
        })
        chunks.append(InternalChunk(
            text=chunk_text,
            doc_id=str(path),
            type="ato_module",
            metadata={
                "corpus": "atopile_examples",
                "source": str(path),
                "project": project,
                "module_name": name,
                "imports": IMPORT_RE.findall(content),
                "stdlib_types_used": stdlib_used,
            },
        ))
    return chunks


def ingest_atopile_examples(examples_root: Path) -> list[InternalChunk]:
    """Walk every .ato under examples_root, skip build/dot-dirs."""
    all_chunks = []
    for ato_path in examples_root.rglob("*.ato"):
        if any(p.startswith(".") or p in {"build", "node_modules"}
               for p in ato_path.parts):
            continue
        project = ato_path.relative_to(examples_root).parts[0]
        all_chunks.extend(chunk_ato_file(ato_path, project))
    return all_chunks
```

---

## `rag/enrich.py`

```python
"""Add metadata + LLM summaries to raw chunks."""
import asyncio
import hashlib
import re
from datetime import datetime, timezone
from langchain_anthropic import ChatAnthropic
from .chunk.datasheet_chunker import RawChunk

_summarizer = ChatAnthropic(model="claude-haiku-4-5", max_tokens=80, temperature=0)

SUMMARY_PROMPT = """Summarize this engineering document chunk in ONE sentence (≤25 words).
Focus on what specific fact, parameter, rule, or design pattern it covers.
Be concrete — name the parameter, the threshold, the technique, not "discusses X".

Chunk:
{content}

One-sentence summary:"""

# Known MPN regex patterns — extend as you encounter new manufacturers
MPN_PATTERNS = [
    (r"\b(STM32[A-Z]\d+[A-Z0-9]+)\b", "STMicroelectronics"),
    (r"\b(nRF\d{5}-[A-Z]+-?R?\d?)\b", "Nordic Semiconductor"),
    (r"\b(TLV\d{3,4}[A-Z0-9-]+)\b", "Texas Instruments"),
    (r"\b(LM[0-9]{2,4}[A-Z0-9-]+)\b", "Texas Instruments"),
    (r"\b(LT\d{4}[A-Z]?(?:-\d+)?)\b", "Analog Devices / Linear"),
    (r"\b(MAX\d{3,5}[A-Z0-9-]+)\b", "Analog Devices / Maxim"),
    (r"\b(ATmega\d+[A-Z0-9-]+)\b", "Microchip / Atmel"),
    (r"\b(ESP32-[A-Z0-9-]+)\b", "Espressif"),
]

def extract_mpn_and_manufacturer(text: str) -> tuple[str | None, str | None]:
    for pattern, manufacturer in MPN_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1), manufacturer
    return None, None

def _stable_chunk_id(source_hash: str, section_path: str, content: str) -> str:
    """Deterministic ID. Stable across re-ingests if section + content prefix unchanged."""
    content_prefix = content[:200]
    composite = f"{source_hash}|{section_path}|{content_prefix}"
    return hashlib.sha256(composite.encode()).hexdigest()[:24]

async def _summarize_one(content: str) -> str:
    truncated = content[:2000]
    response = await _summarizer.ainvoke(SUMMARY_PROMPT.format(content=truncated))
    return response.content.strip()

async def _summarize_batch(contents: list[str], concurrency: int = 20) -> list[str]:
    sem = asyncio.Semaphore(concurrency)
    async def _wrapped(c):
        async with sem:
            return await _summarize_one(c)
    return await asyncio.gather(*(_wrapped(c) for c in contents))

async def enrich(
    raw_chunks: list[RawChunk],
    corpus: str,
    source_path: str,
    source_hash: str,
    doc_type: str,
) -> list[dict]:
    """Returns enriched chunk dicts ready for embedding + storage."""
    summaries = await _summarize_batch([c.content for c in raw_chunks])

    enriched = []
    for raw, summary in zip(raw_chunks, summaries):
        metadata = {
            "chunk_id": _stable_chunk_id(source_hash, raw.section_path, raw.content),
            "corpus": corpus,
            "source": source_path,
            "source_hash": source_hash,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "page_start": raw.page_start,
            "page_end": raw.page_end,
            "section": raw.section_path,
            "summary": summary,
            "token_count": len(raw.content) // 4,
            "doc_type": doc_type,
        }

        # Type-specific enrichment
        if doc_type in {"datasheet", "app_note"}:
            mpn, manufacturer = extract_mpn_and_manufacturer(raw.content[:3000])
            metadata["mpn"] = mpn
            metadata["manufacturer"] = manufacturer

        metadata.update(raw.extras)

        enriched.append({
            "content": raw.content,
            "metadata": metadata,
        })
    return enriched
```

---

## `rag/embed.py`

```python
"""Embedding provider with batching and async."""
import os
from typing import Protocol
import voyageai
from .config import EMBED_MODEL, EMBED_BATCH_SIZE

class Embedder(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...

class VoyageEmbedder:
    def __init__(self, model: str = EMBED_MODEL):
        self.client = voyageai.AsyncClient(api_key=os.environ["VOYAGE_API_KEY"])
        self.model = model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            result = await self.client.embed(
                batch, model=self.model, input_type="document"
            )
            out.extend(result.embeddings)
        return out

    async def embed_query(self, text: str) -> list[float]:
        result = await self.client.embed(
            [text], model=self.model, input_type="query"
        )
        return result.embeddings[0]
```

---

## `rag/store.py`

```python
"""Qdrant store + BM25 sidecar."""
import pickle
from pathlib import Path
from qdrant_client import AsyncQdrantClient, models
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from .config import QDRANT_URL, EMBED_DIM, DATA_ROOT

PAYLOAD_INDEXED_FIELDS = [
    ("mpn", models.PayloadSchemaType.KEYWORD),
    ("manufacturer", models.PayloadSchemaType.KEYWORD),
    ("standard", models.PayloadSchemaType.KEYWORD),
    ("clause", models.PayloadSchemaType.KEYWORD),
    ("revision", models.PayloadSchemaType.KEYWORD),
    ("doc_type", models.PayloadSchemaType.KEYWORD),
    ("source_hash", models.PayloadSchemaType.KEYWORD),
    ("page_start", models.PayloadSchemaType.INTEGER),
]

class CorpusStore:
    def __init__(self, corpus: str):
        self.corpus = corpus
        self.client = AsyncQdrantClient(url=QDRANT_URL)
        self.bm25_path = DATA_ROOT / "bm25" / f"{corpus}.pkl"

    async def ensure_collection(self):
        existing = await self.client.collection_exists(self.corpus)
        if not existing:
            await self.client.create_collection(
                self.corpus,
                vectors_config=models.VectorParams(
                    size=EMBED_DIM, distance=models.Distance.COSINE,
                ),
            )
            for field, schema in PAYLOAD_INDEXED_FIELDS:
                await self.client.create_payload_index(
                    self.corpus, field_name=field, field_schema=schema,
                )

    async def upsert(self, chunks: list[dict], embeddings: list[list[float]]):
        points = [
            models.PointStruct(
                id=c["metadata"]["chunk_id"],
                vector=v,
                payload={**c["metadata"], "content": c["content"]},
            )
            for c, v in zip(chunks, embeddings)
        ]
        await self.client.upsert(self.corpus, points=points)

    async def has_source(self, source_hash: str) -> bool:
        result, _ = await self.client.scroll(
            self.corpus,
            scroll_filter=models.Filter(must=[
                models.FieldCondition(
                    key="source_hash", match=models.MatchValue(value=source_hash),
                ),
            ]),
            limit=1,
        )
        return len(result) > 0

    async def all_chunks_for_corpus(self) -> list[Document]:
        """For rebuilding BM25 index."""
        result, _ = await self.client.scroll(self.corpus, limit=100_000)
        return [
            Document(
                page_content=p.payload["content"],
                metadata={k: v for k, v in p.payload.items() if k != "content"},
            )
            for p in result
        ]

    async def rebuild_bm25(self):
        docs = await self.all_chunks_for_corpus()
        retriever = BM25Retriever.from_documents(docs)
        retriever.k = 20
        self.bm25_path.parent.mkdir(parents=True, exist_ok=True)
        self.bm25_path.write_bytes(pickle.dumps(retriever))

    def load_bm25(self) -> BM25Retriever | None:
        if not self.bm25_path.exists():
            return None
        return pickle.loads(self.bm25_path.read_bytes())
```

---

## `rag/ingest.py`

```python
"""Top-level ingest CLI."""
import argparse
import asyncio
import hashlib
from pathlib import Path

from .config import DOC_TYPE_TO_CORPUS
from .classify import classify
from .parse import parse
from .chunk import chunk_dispatch
from .enrich import enrich
from .embed import VoyageEmbedder
from .store import CorpusStore

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

async def ingest_one(path: Path, force: bool = False) -> dict:
    doc_type = classify(path)
    corpus = DOC_TYPE_TO_CORPUS[doc_type]
    source_hash = _sha256(path)

    store = CorpusStore(corpus)
    await store.ensure_collection()

    if not force and await store.has_source(source_hash):
        return {"status": "skipped", "reason": "unchanged", "path": str(path)}

    parsed_md = parse(path, doc_type)
    raw_chunks = chunk_dispatch(parsed_md, doc_type)
    enriched = await enrich(
        raw_chunks,
        corpus=corpus,
        source_path=str(path),
        source_hash=source_hash,
        doc_type=doc_type,
    )

    embedder = VoyageEmbedder()
    embeddings = await embedder.embed_documents([c["content"] for c in enriched])

    await store.upsert(enriched, embeddings)
    await store.rebuild_bm25()

    return {
        "status": "ingested",
        "path": str(path),
        "corpus": corpus,
        "doc_type": doc_type,
        "chunks": len(enriched),
    }

async def ingest_many(paths: list[Path], force: bool = False, concurrency: int = 4):
    sem = asyncio.Semaphore(concurrency)
    async def _one(p):
        async with sem:
            try:
                return await ingest_one(p, force)
            except Exception as e:
                return {"status": "error", "path": str(p), "error": str(e)}
    results = await asyncio.gather(*(_one(p) for p in paths))
    for r in results:
        print(r)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(ingest_many(args.paths, args.force, args.concurrency))

if __name__ == "__main__":
    main()
```

Usage:

```bash
# Single file
python -m ee_agent.rag.ingest data/datasheets/TLV713P.pdf

# A whole directory, 4-way parallel
python -m ee_agent.rag.ingest data/datasheets/*.pdf --concurrency 4

# Force re-ingest after parser changes
python -m ee_agent.rag.ingest data/standards/IPC-2221B.pdf --force
```

---

## `rag/retriever.py` (query-time, briefly)

```python
"""The retriever called by the `rag_search` tool at query time."""
from langchain.retrievers import EnsembleRetriever
from langchain.retrievers.contextual_compression import ContextualCompressionRetriever
from langchain_cohere import CohereRerank
from .store import CorpusStore
from .embed import VoyageEmbedder

class CorpusRetriever:
    def __init__(self, corpus: str):
        self.store = CorpusStore(corpus)
        self.embedder = VoyageEmbedder()
        self.bm25 = self.store.load_bm25()
        self.reranker = CohereRerank(model="rerank-english-v3.0", top_n=5)

    async def retrieve(self, query: str, top_k: int = 5, filter: dict | None = None):
        # Dense retrieval
        query_vec = await self.embedder.embed_query(query)
        dense_hits = await self.store.client.search(
            collection_name=self.corpus,
            query_vector=query_vec,
            query_filter=self._build_filter(filter),
            limit=top_k * 4,
        )
        # Sparse retrieval
        sparse_hits = self.bm25.invoke(query) if self.bm25 else []
        # Merge with reciprocal rank fusion
        candidates = reciprocal_rank_fusion(dense_hits, sparse_hits, k=60)
        # Rerank top-N with Cohere
        reranked = self.reranker.compress_documents(candidates, query=query)
        return reranked[:top_k]
```

A full `rag_search` tool then composes one or more `CorpusRetriever` instances based on the `corpus` arg.

---

## A note on what's left out

This skeleton is missing:
- The classifier's LLM fallback (~2% of docs)
- Full implementations of `standard_parser`, `textbook_parser`, `markdown_parser`
- The full chunker dispatch and `standard_chunker` / `textbook_chunker` / `markdown_chunker`
- Eval set runner and CI integration
- The retriever's RRF merge and corpus router

Each is a 50-100 line module. The point of the skeleton is the *seams*: each step is independently swappable, with a small interface between. When LlamaParse v5 ships, you swap one parser file. When voyage-4 ships, you swap one embedder file. The pipeline stays stable.
