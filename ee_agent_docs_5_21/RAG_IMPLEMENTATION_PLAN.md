# RAG Implementation — Step-by-Step Build Plan

> **Read this first if you're a Claude Code session starting work.** The whole EE agent depends on RAG quality. We're building the retriever before the agents. This file is a build plan you can execute in order.

> **Option C alignment.** Under the Option C architecture (see `09_HARNESS_ANALYSIS.md`, `08_PROJECT_PLAN.md`), the work in this file maps to **milestone 4** of the project plan. Everything in this doc still applies — the retrieval pipeline, chunkers, eval set, code skeleton. The only architectural shift is that the final `rag_search` function gets wrapped as an atopile tool (registered via the `_register_tool` decorator in `src/atopile/server/agent/_ee/tools_rag.py`) rather than as a LangChain `@tool` callable. The retriever package itself (`src/ee_agent_rag/`) lives outside atopile's agent module and is framework-agnostic. See `06_ATOPILE_INTEGRATION.md` §5 for the tool registration mechanism.

---

## What "done" looks like for this phase

A working `rag_search(query, corpus, top_k)` Python function that:
1. Returns relevant chunks from a Qdrant collection
2. Combines dense (voyage-3) + sparse (BM25) retrieval via RRF
3. Reranks with Cohere
4. Returns each chunk with full citation metadata (source, page, section, MPN/clause)
5. Has ≥80% recall@5 on a 30-question datasheet eval set
6. Is callable from any future agent as a LangChain `@tool`

That's it. No agents yet. No orchestration. Just retrieval that works.

---

## Pre-flight — environment setup (15 min)

Before any code, get the runtime ready.

```bash
# 1. Project setup
mkdir ee_agent && cd ee_agent
git init
uv init --python 3.11
uv add deepagents langgraph langchain langchain-anthropic langchain-community \
       langchain-cohere voyageai qdrant-client llama-parse rank-bm25 pdfminer.six \
       pydantic python-dotenv

# 2. .env file
cat > .env <<'EOF'
ANTHROPIC_API_KEY=...
VOYAGE_API_KEY=...
COHERE_API_KEY=...
LLAMA_CLOUD_API_KEY=...
LANGCHAIN_API_KEY=...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=ee-agent-rag
QDRANT_URL=http://localhost:6333
EE_DATA_ROOT=./data
EOF

# 3. Qdrant via Docker
docker run -d -p 6333:6333 -p 6334:6334 \
    -v $(pwd)/qdrant_storage:/qdrant/storage \
    --name qdrant qdrant/qdrant

# 4. Verify Qdrant is up
curl http://localhost:6333/healthz

# 5. Create folder layout
mkdir -p data/{datasheets,standards,app_notes,textbooks,internal,.parsed_cache}
mkdir -p src/ee_agent/rag/{parse,chunk,eval/datasets}
touch src/ee_agent/__init__.py src/ee_agent/rag/__init__.py
```

**API keys you actually need for v1:**
- `ANTHROPIC_API_KEY` — model calls
- `VOYAGE_API_KEY` — embeddings ($0.06/1M tokens, free tier 50M)
- `COHERE_API_KEY` — reranker (free tier 1k calls/month, plenty for dev)
- `LLAMA_CLOUD_API_KEY` — PDF parsing (free tier 10k credits/day)

If you don't want to pay for LlamaParse, the fallback is `unstructured` (slower, lower quality on tables); the pipeline supports both.

---

## Step 1 — Seed the corpus (30 min)

You can't test retrieval without documents. Before touching code, download a real working set:

```bash
cd data/datasheets
# Pull 10 datasheets — high-volume parts you'd actually design with
wget https://www.ti.com/lit/ds/symlink/tlv713p.pdf -O tlv713p.pdf
wget https://www.ti.com/lit/ds/symlink/tps62840.pdf -O tps62840.pdf
wget https://www.st.com/resource/en/datasheet/stm32f103c8.pdf -O stm32f103c8.pdf
wget https://infocenter.nordicsemi.com/pdf/nRF52840_PS_v1.10.pdf -O nrf52840.pdf
# ... etc. Aim for 10-20 diverse parts (LDO, buck, MCU, sensor, transceiver)

cd ../standards
# IPC standards are paid. For dev, use what you have legally:
# - Internal copies of IPC-2221, IPC-7351 if your company has them
# - MIL-STD documents are public domain — start there
wget https://everyspec.com/MIL-STD/MIL-STD-0300-0499/MIL-STD-461G_36275/download -O mil-std-461g.pdf

cd ../textbooks
# Use only what you legally own. For dev/eval purposes, a single chapter
# excerpt is enough to validate the pipeline.
```

**For Claude Code:** Don't pull copyrighted material you don't own. Use public sources or company-licensed material. The pipeline doesn't care what's in the PDFs — start with 10 datasheets and prove the loop works.

---

## Step 2 — Implement the pipeline (3-4 hours)

The code skeleton in `rag/INGESTION_CODE_SKELETON.md` is the spec. Build each file as a small unit. Order matters:

### 2.1 — `src/ee_agent/rag/config.py`
Constants, paths, baselines. ~30 lines. **Copy directly from the skeleton.**

### 2.2 — `src/ee_agent/rag/classify.py`
Filename + first-page text → DocType. ~60 lines. **Copy from skeleton.**

**Test it:**
```python
from pathlib import Path
from ee_agent.rag.classify import classify

assert classify(Path("data/datasheets/tlv713p.pdf")) == "datasheet"
assert classify(Path("data/standards/mil-std-461g.pdf")) == "standard"
print("classify OK")
```

### 2.3 — `src/ee_agent/rag/parse/datasheet_parser.py`
LlamaParse wrapper for datasheets. ~50 lines. **Copy from skeleton.** Confirm cache directory exists.

**Test it on one datasheet:**
```python
import asyncio
from pathlib import Path
from ee_agent.rag.parse.datasheet_parser import parse_datasheet

md = parse_datasheet(Path("data/datasheets/tlv713p.pdf"))
print(f"Parsed {len(md)} chars")
print(md[:2000])
```

Visually verify: do "Electrical Characteristics" tables appear as markdown tables? Are section headings preserved as `##`? Page markers present? If not, tune `DATASHEET_INSTRUCTION` in the parser.

### 2.4 — `src/ee_agent/rag/chunk/datasheet_chunker.py`
Section-aware splitter. ~80 lines. **Copy from skeleton.**

**Test it on the same datasheet:**
```python
from ee_agent.rag.chunk.datasheet_chunker import chunk_datasheet

chunks = chunk_datasheet(md)
for i, c in enumerate(chunks):
    print(f"[{i}] {c.section_path} — {c.page_start}-{c.page_end} — {len(c.content)} chars")
```

Look at the section paths. You should see "Electrical Characteristics", "Absolute Maximum Ratings", "Typical Application", etc. as single chunks. If a critical table is split across chunks, the parser instruction needs tightening.

### 2.5 — `src/ee_agent/rag/enrich.py`
Add metadata + Haiku summaries. ~80 lines. **Copy from skeleton.**

**Test it:**
```python
enriched = asyncio.run(enrich(
    chunks, corpus="datasheets",
    source_path="data/datasheets/tlv713p.pdf",
    source_hash="abc123",
    doc_type="datasheet",
))
for e in enriched[:3]:
    print(e["metadata"]["section"])
    print("  summary:", e["metadata"]["summary"])
    print("  mpn:", e["metadata"].get("mpn"))
```

Confirm: MPN extracted, summaries look concrete (not "discusses the chip"), chunk IDs deterministic.

### 2.6 — `src/ee_agent/rag/embed.py`
Voyage-3 wrapper. ~30 lines. **Copy from skeleton.**

**Test it:**
```python
from ee_agent.rag.embed import VoyageEmbedder
embedder = VoyageEmbedder()
vecs = asyncio.run(embedder.embed_documents(["test passage 1", "test passage 2"]))
assert len(vecs) == 2 and len(vecs[0]) == 1024
print("embed OK")
```

### 2.7 — `src/ee_agent/rag/store.py`
Qdrant wrapper + BM25 sidecar. ~80 lines. **Copy from skeleton.**

### 2.8 — `src/ee_agent/rag/ingest.py`
CLI that ties it all together. ~80 lines. **Copy from skeleton.**

**Run it:**
```bash
uv run python -m ee_agent.rag.ingest data/datasheets/tlv713p.pdf
```

If this completes without error and prints `{"status": "ingested", "chunks": N}`, **the ingestion half is working.** Run it on all 10 datasheets:

```bash
uv run python -m ee_agent.rag.ingest data/datasheets/*.pdf --concurrency 4
```

### 2.9 — `src/ee_agent/rag/retriever.py`
The query-time side. ~120 lines. This isn't in the skeleton yet — write it from `agents/05_RAG.md`.

Core function:
```python
async def rag_search(query: str, corpus: str = "datasheets", top_k: int = 5,
                    filter: dict | None = None) -> list[dict]:
    """Hybrid retrieval + Cohere rerank. Returns chunks with citations."""
    embedder = VoyageEmbedder()
    store = CorpusStore(corpus)
    bm25 = store.load_bm25()

    # Dense retrieval
    qvec = await embedder.embed_query(query)
    dense_hits = await store.client.search(
        collection_name=corpus,
        query_vector=qvec,
        query_filter=_build_filter(filter),
        limit=top_k * 4,
    )

    # Sparse retrieval
    sparse_hits = bm25.invoke(query) if bm25 else []

    # RRF fusion
    fused = reciprocal_rank_fusion(dense_hits, sparse_hits, k=60)

    # Cohere rerank
    reranker = CohereRerank(model="rerank-english-v3.0", top_n=top_k)
    reranked = reranker.compress_documents(fused[:20], query=query)

    return [
        {
            "content": doc.page_content,
            "score": doc.metadata.get("relevance_score"),
            "citation": _build_citation(doc.metadata),
        }
        for doc in reranked
    ]
```

`reciprocal_rank_fusion` is ~15 lines — see `tim-ponomarev/hybrid-rag` for a reference implementation.

**Test it:**
```python
results = asyncio.run(rag_search(
    "What's the typical quiescent current of the TLV713P?",
    corpus="datasheets",
))
for r in results:
    print(r["citation"], "—", r["content"][:200])
```

You should see the TLV713P's electrical characteristics chunk as the top result. If not, the retriever needs tuning — usually the BM25 weight or the rerank `top_n`.

---

## Step 3 — Build the eval set (1-2 hours)

Without an eval set you can't measure anything. Write 30 questions covering your 10 datasheets:

`src/ee_agent/rag/eval/datasets/datasheets.jsonl`:
```jsonl
{"query": "What is the typical quiescent current of the TLV713P?", "must_contain_mpn": "TLV713P", "must_contain_section": "Electrical Characteristics"}
{"query": "TPS62840 minimum input voltage", "must_contain_mpn": "TPS62840", "must_contain_section": "Recommended Operating Conditions"}
{"query": "STM32F103 FLASH size", "must_contain_mpn": "STM32F103"}
...
```

Three eval styles for technical retrieval:
- **MPN + section** — most retrievals are like this. The right answer is "the electrical-characteristics chunk for the TLV713P."
- **Parameter-only** — "Find me an LDO with PSRR > 60 dB at 1 kHz." The right answer is *any* LDO meeting that spec.
- **Methodology** — "How do I calculate stability margins?" The right answer is a textbook or app-note chunk.

Start with 30 of the first kind. Add the others as you grow the corpus.

### Eval runner

`src/ee_agent/rag/eval/runner.py`:
```python
async def run_eval(corpus: str, eval_set_path: Path, top_k: int = 5) -> dict:
    queries = [json.loads(l) for l in eval_set_path.read_text().splitlines() if l.strip()]
    hits = 0
    for q in queries:
        results = await rag_search(q["query"], corpus=corpus, top_k=top_k)
        passed = True
        if "must_contain_mpn" in q:
            passed &= any(r["citation"].get("mpn") == q["must_contain_mpn"] for r in results)
        if "must_contain_section" in q:
            passed &= any(q["must_contain_section"] in r["citation"].get("section", "") for r in results)
        if passed:
            hits += 1
        else:
            print(f"MISS: {q['query']}")
    return {"corpus": corpus, "recall_at_5": hits / len(queries), "total": len(queries)}
```

Run it:
```bash
uv run python -m ee_agent.rag.eval.runner --corpus datasheets
```

**Target: ≥80% recall@5 on the datasheets eval.** If you're below, the fixes are usually (in order of impact):
1. Parser is collapsing tables (LlamaParse instruction needs tightening)
2. Chunker is splitting mid-section (check `_split_at_headings` regex)
3. BM25 weight too low / too high (try 0.4/0.6 split first, then 0.3/0.7)
4. Reranker not catching enough candidates (increase pre-rerank `limit` to `top_k * 8`)
5. Voyage `input_type` mismatched (use `"document"` at ingest, `"query"` at retrieval)

---

## Step 4 — Wrap as a LangChain tool (15 min)

This is the final integration so future agents can use it.

`src/ee_agent/tools/rag.py`:
```python
from langchain_core.tools import tool
from ee_agent.rag.retriever import rag_search as _rag_search

@tool
async def rag_search(
    query: str,
    corpus: str = "datasheets",
    top_k: int = 5,
    filter: dict | None = None,
) -> str:
    """Search the engineering knowledge base.

    Args:
        query: natural-language search query
        corpus: which corpus to search — one of: datasheets, app_notes, standards, textbooks, internal_standards
        top_k: number of chunks to return after rerank
        filter: optional metadata filter, e.g. {"mpn": "TLV713P"} or {"standard": "IPC-2221B"}

    Returns formatted, cited chunks ready for inclusion in an LLM prompt.
    """
    results = await _rag_search(query, corpus, top_k, filter)
    return "\n\n---\n\n".join(
        f"[{r['citation']['source']} · p.{r['citation'].get('page')}"
        f" · {r['citation'].get('section', '')}]\n{r['content']}"
        for r in results
    )
```

**Test the tool end-to-end:**
```python
from ee_agent.tools.rag import rag_search
print(await rag_search.ainvoke({"query": "LDO PSRR for low-noise analog", "corpus": "datasheets"}))
```

---

## Step 5 — Done check (15 min)

Before moving to the next phase, verify the whole loop:

```bash
# 1. Ingestion working
uv run python -m ee_agent.rag.ingest data/datasheets/*.pdf
# Expect: all PDFs ingested without error

# 2. Retrieval working
uv run python -c "
import asyncio
from ee_agent.rag.retriever import rag_search
r = asyncio.run(rag_search('TLV713P quiescent current', 'datasheets'))
for x in r: print(x['citation'])
"
# Expect: top result is the TLV713P electrical characteristics chunk

# 3. Eval passing
uv run python -m ee_agent.rag.eval.runner --corpus datasheets
# Expect: recall@5 ≥ 0.80
```

If all three pass, **the RAG foundation is done**. Move to Phase 2 (parts agent).

---

## What to do if you get stuck

**LlamaParse times out:** Their free tier has rate limits. Either wait, get a paid key, or fall back to `unstructured` (`pip install "unstructured[pdf]"`). The pipeline supports both.

**Qdrant connection refused:** Container died. `docker ps -a` to check, `docker start qdrant`.

**Voyage embeds return wrong dimension:** You're using a non-`voyage-3` model. Check `VoyageEmbedder.__init__`.

**Cohere rerank returns empty:** API key wrong or out of free-tier calls. Fall back to local rerank with `sentence-transformers/cross-encoder/ms-marco-MiniLM-L-6-v2`.

**Recall is below 50%:** Something is structurally wrong. Order to debug:
1. Inspect a chunk in Qdrant directly: `client.scroll(collection, limit=1)` — does the payload look right?
2. Run BM25 alone with the failing query — does it find the chunk?
3. Run dense alone — does it find the chunk?
4. If both find it, the rerank is dropping it. Increase pre-rerank limit.
5. If neither finds it, the chunk is bad — go back to parser/chunker.

---

## Reference repos worth pulling open in another tab

While building, keep these open for reference (full notes in `references/REFERENCES.md`):

- `tim-ponomarev/hybrid-rag` — copy the RRF fusion code
- `mburaksayici/RAG-Boilerplate` — copy the eval harness structure
- `langchain-ai/rag-from-scratch` — the indexing-strategies notebook for chunking ideas
- `yaqwsx/jlcparts` — for later (parts API), not RAG

---

## Acceptance criteria for handoff

When this phase ships, you should be able to demo:

1. `uv run python -m ee_agent.rag.ingest data/datasheets/new_part.pdf` — ingests in under 60s
2. `await rag_search("query about new_part", "datasheets")` — returns cited chunks
3. `uv run python -m ee_agent.rag.eval.runner --corpus datasheets` — prints recall@5 ≥ 0.80
4. Every result includes `source`, `page`, `section`, `mpn` in citation

If you can do all four, the RAG foundation is ready for the parts agent to be built on top of it.

---

## What this phase does NOT include (intentionally)

To keep scope tight, deferred to later phases:
- Multi-corpus query routing (we hardcode `corpus` for now)
- Query rewriting / HyDE / step-back
- The standards corpus (start with datasheets only)
- The textbook corpus (lowest leverage per dollar)
- Re-embed sweeps and corpus migration
- Production async ingestion (Celery, etc.)
- The "summary" field's use in retrieval (it's stored, not yet queried against)

Each of those is a focused follow-up once the foundation is solid.
