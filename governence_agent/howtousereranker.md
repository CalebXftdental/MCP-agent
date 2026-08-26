# How to Use the Reranker (bge-reranker-v2-m3)

**Status (2026-08-21):** Deployed and live at the endpoint below, verified end-to-end
(external HTTP call, auth enforced, correct scores). **Not yet wired into this
codebase** — `personal_knowledge_store.py` and `knowledge_store.py` still return
unranked (embedding-cosine / TF-IDF / Azure AI Search) results directly. This doc
covers how to call the deployed service; integrating the actual call sites is
separate follow-up work (see §5).

## 1. What this is and why

Two-stage retrieval pipeline for the personal + company knowledge bases (the "home
chatbot" KB path — **not** the workflow copilot's tool/node selection, which is a
separate, harder problem the same reranker was tested against and is *not*
recommended for yet; see §6):

```
embedding retrieval (existing, unchanged) -> rerank (this service) -> LLM
```

A cross-encoder reranker reads the full query + document text jointly, which is far
more accurate than comparing two independently-computed embedding vectors — but too
expensive to run over an entire KB. So embedding search still does the cheap,
approximate narrowing (e.g. top-30 out of thousands of chunks), and this service does
the expensive, accurate re-ordering of just that narrowed set (e.g. down to the
actual top-5/top-10 handed to the LLM).

Validated before deployment on a 70-document synthetic business-KB eval (real
Frontier Dental SOP content mixed with synthetic business-logic notes covering vendor
contacts, customer terms/near-duplicates, escalation policy, and deliberate
superseded-note traps): **20/20 top-10 accuracy, 20/20 top-5, 19/20 top-1** — the one
top-1 miss is the superseded-note case discussed in §5, not a ranking defect.

## 2. Endpoint

| | |
|---|---|
| **Base URL** | `http://20.120.220.8:8091` |
| **Model** | bge-reranker-v2-m3, Q8_0 quantization |
| **Auth** | `Authorization: Bearer <RERANKER_API_KEY>` — get the key from whoever deployed this service (see §4 for where it's documented). **Don't hardcode it in code or commit it to this repo** — add it to `gateway/.env.local` as `RERANKER_API_KEY`, matching the existing `GOVERNANCE_ANTHROPIC_API_KEY`/`GOVERNANCE_CHAT_API_KEY` pattern already in that file. |
| **Hardware** | CPU-only, `numactl`-pinned to 4 NUMA nodes (48 threads), on the same VM as the Qwen3.8-27B chat LLM but fully isolated from it — separate systemd service, separate CPU cores, never touches the GPU or the LLM's process. |

## 3. Calling it

### Request

```
POST /v1/rerank
Authorization: Bearer <RERANKER_API_KEY>
Content-Type: application/json

{
  "query": "Can a Canadian customer still mail in a check for payment?",
  "documents": ["...chunk 1 text...", "...chunk 2 text...", "...chunk N text..."],
  "top_n": 10
}
```

`documents` is a flat array of chunk **text**, not ids — you're responsible for
mapping the returned `index` back to whatever id/metadata you tracked on your side
before calling this.

### Response

```json
{
  "results": [
    { "index": 4, "relevance_score": 5.31 },
    { "index": 0, "relevance_score": 2.87 },
    { "index": 12, "relevance_score": -1.02 }
  ]
}
```

Pre-sorted best-first. `relevance_score` is a raw logit, not a probability — use it
for relative ranking within one call, not as an absolute confidence threshold
compared across different queries.

### Python example

```python
import httpx

async def rerank(query: str, documents: list[str], top_n: int = 10) -> list[int]:
    """Calls the reranker; returns document indices (into `documents`) in
    best-first order."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{RERANKER_BASE_URL}/v1/rerank",
            headers={"Authorization": f"Bearer {RERANKER_API_KEY}"},
            json={"query": query, "documents": documents, "top_n": top_n},
        )
        resp.raise_for_status()
        return [r["index"] for r in resp.json()["results"]]
```

### Latency (measured, not estimated)

| Scenario | Docs | Latency |
|---|---|---|
| Direct external call, small request | 2 | 356ms |
| Through an SSH tunnel from an external machine (real network overhead on top of compute) | 30 | ~750-800ms |
| On-VM, no network hop | 30 | ~830-1,000ms range (format-dependent) |

This codebase's gateway calls the chat LLM from Azure App Service in Canada East,
while this reranker (and the LLM) run on a VM in West US 2 — a real inter-region
network hop, not a local call. Expect something between the tunnel and on-VM numbers
above, likely closer to the faster end since Azure's inter-region backbone generally
outperforms a generic internet path — but this has **not been directly measured**
from the actual Canada East gateway location yet. Re-measure from there before
treating any specific number as a committed SLA.

## 4. Where the rest of the deployment detail lives

Full deployment history, the BF16-vs-Q8_0 hardware benchmarking (a genuinely
hardware-specific result — measured, not assumed, and don't assume it transfers if
this ever moves to different hardware), the AMX investigation (it turned out *not* to
explain Q8_0's speed advantage — general AVX512-VNNI support did), NUMA-scaling data,
the systemd unit, and the NSG change are documented alongside the LLM's own
deployment docs, not duplicated in this repo:

- `Qwen3.6_27B/deploy.md` — full deployment writeup and tuning history, dated 2026-08-21
- `Qwen3.6_27B/how_to_use.md` — the same call reference as §3 above, plus the measured latency table

Server management (`systemctl status/restart reranker`, `journalctl -u reranker -f`)
requires SSH access to that VM — ask whoever has it if the service needs attention.

## 5. Integration status — not yet wired in

This service is live and tested standalone, but **no code in this repo calls it
yet**. The two real integration points:

- `governance_core/personal_knowledge_store.py` — `search()` currently returns
  embedding-cosine or TF-IDF top-k directly, no reranking pass.
- `governance_core/knowledge_store.py` / `mcp-knowledge/app.py` — company KB search,
  same gap (and note: local dev currently points this at a **live** Azure AI Search
  index via direct config, not a local store — see that file's own comments).

Wiring this in means: retrieve top-20 to top-30 as today (unchanged), pass those
candidates + the query to `rerank()` above, take the top-5 to top-10 by returned
order, and use *that* as what gets handed to the LLM. **Add a fallback path**
(pass the embedding results straight through unranked) for when the reranker
endpoint is unreachable — don't let a reranker outage take down KB search entirely.

**Known limitation to carry into the integration**: the reranker scores semantic
relevance, not recency or authority. If personal/company KB notes ever get
superseded (a policy changes, an old note isn't deleted), the reranker will often
rank the *stale* note above the current one if it happens to be more lexically
similar to the query — confirmed directly in testing (a "can Canadian customers mail
a check" query ranked a deprecated "yes" note above the current "no, EFT only" note,
reproducibly, across every format and candidate-pool size tested). Either add
explicit `superseded_by`/`deprecated` filtering before reranking, or don't assume
reranking alone resolves contradictory KB content — it won't.

## 6. Not for copilot tool/node selection (yet)

This same reranker was also tested against the workflow copilot's tool-selection
problem — picking the right tool out of the full ~71-tool registry (`gateway/app.py`),
plus the 7 workflow node kinds (`GraphNode.kind` in `workflow_graph_models.py`).
Results were meaningfully worse than the KB case, and worse in a structurally
informative way:

- **Tool selection alone**: 65% top-1 accuracy (vs. ~90%+ on KB content), though still
  a clean 100% top-5/top-10. Tool descriptions are short and templated ("Get
  header-level details for one X by Y number"), so near-identical sibling tools
  (e.g. the three "list the line items in X" tools across PO/AP/AR, or the four
  "create X report" tools) are genuinely hard to rank against each other on lexical
  overlap alone.
- **Mixing the 7 node kinds into the same pool as the 71 tools caused a real failure,
  not just a near-miss**: a "for each customer, run a sub-workflow individually"
  query ranked `node_loop` at position **22** — completely outside top-10 — because
  dozens of tool descriptions mention "customer" repeatedly and out-lexically-compete
  the node's more technical, config-phrased description. All 7 node kinds landed
  below rank 20 for that query; this wasn't a loop-vs-join mixup, the entire node
  family lost to the entire tool family on raw word overlap.

**Conclusion: don't reuse this same reranking call for copilot tool/node selection
without more work.** Node-kind selection probably shouldn't compete in the same
candidate pool as tool selection at all (there are only 7 kinds — that's closer to a
cheap classification problem than something needing a reranker), and tool
descriptions may need rewriting toward more natural task-phrased language before
reranking-based tool selection is trustworthy on its own. Not built, not recommended
as-is.

---

*Written 2026-08-21 after local model comparison (bge-reranker-v2-m3 vs.
Qwen3-Reranker-4B/0.6B — the latter's 0.6B checkpoint hit a known llama.cpp bug,
[ggml-org/llama.cpp#16407](https://github.com/ggml-org/llama.cpp/issues/16407),
producing degenerate scores regardless of GGUF source), business-KB accuracy/latency
testing at increasing scale and realism, and production deployment to the
Qwen3.6/3.8-27B VM's idle CPU capacity (NUMA nodes 1-4, avoiding node 0 where the
LLM's own scheduler and CUDA host-memory footprint concentrate, and never touching
the GPU).*
