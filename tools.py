"""SAHAYA tools.

Deterministic capabilities the agents call, plus the deterministic backstop
that validates the adjudicator's arithmetic in code. Agents reason; these
functions are controls.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent_framework import tool
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

DATA_DIR = Path(__file__).parent / "data"
INDEX_NAME = "sahaya-norms"


# ---------------------------------------------------------------------------
# Lazy clients (nothing touches the network at import time)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _search_client() -> SearchClient:
    return SearchClient(
        endpoint=os.environ["SEARCH_ENDPOINT"],
        index_name=INDEX_NAME,
        credential=AzureKeyCredential(os.environ["SEARCH_KEY"]),
    )


@lru_cache(maxsize=1)
def _embedding_client():
    from openai import AsyncAzureOpenAI

    # NOTE: FoundryEmbeddingClient authenticates with an API key against the
    # separate Foundry MODELS endpoint -- it does not accept AzureCliCredential
    # (that's for FoundryChatClient's project endpoint only). See build_index.py
    # for the same fix, and .env.example for the three env vars this needs.
    return AsyncAzureOpenAI(
        api_key=os.environ["FOUNDRY_MODELS_API_KEY"],
        azure_endpoint=os.environ["FOUNDRY_MODELS_ENDPOINT"],
        api_version="2023-05-15",
    )


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@tool(
    name="search_norms",
    description=(
        "Search the indexed SDRF norms and Kerala Government Orders. Returns "
        "the most relevant passages with document title and page, for citation."
    ),
)
async def search_norms(query: str, top: int = 6) -> str:
    """Hybrid (vector + keyword + semantic) search over the sahaya-norms index."""
    client = _search_client()
    vector_queries = None
    try:
        result = await _embedding_client().embeddings.create(
            model=os.environ["FOUNDRY_EMBEDDING_MODEL"], input=[query]
        )
        vec = result.data[0].embedding
        vector_queries = [VectorizedQuery(vector=vec, k_nearest_neighbors=top, fields="vector")]
    except Exception:
        # Embedding hiccup: degrade to keyword + semantic rather than failing the agent.
        vector_queries = None

    results = client.search(
        search_text=query,
        vector_queries=vector_queries,
        query_type="semantic",
        semantic_configuration_name="sem",
        top=top,
        select=["content", "breadcrumb", "doc_title", "page", "clause_hint"],
    )
    passages = [
        {
            "doc_title": r["doc_title"],
            "page": r["page"],
            "clause_hint": r.get("clause_hint", ""),
            "breadcrumb": r["breadcrumb"],
            "content": r["content"][:1200],
        }
        for r in results
    ]
    if not passages:
        return json.dumps({"passages": [], "note": "no matching norm found in indexed documents"})
    return json.dumps({"passages": passages}, ensure_ascii=False)


@tool(
    name="registry_lookup",
    description=(
        "Look up existing relief claims by masked id and/or village. Returns "
        "matching claims from the claims registry for duplicate screening."
    ),
)
def registry_lookup(masked_id: str = "", village: str = "") -> str:
    registry: list[dict[str, Any]] = json.loads(
        (DATA_DIR / "claims_registry.json").read_text(encoding="utf-8")
    )
    mid = masked_id.strip().lower()
    vil = village.strip().lower()
    matches = []
    for c in registry:
        id_hit = bool(mid) and c.get("masked_id", "").lower() == mid
        vil_hit = bool(vil) and c.get("village", "").lower() == vil
        if id_hit or (id_hit and vil_hit) or (not mid and vil_hit):
            matches.append(c)
        elif vil_hit and not mid:
            matches.append(c)
    # exact-id matches first, then same-village context
    matches.sort(key=lambda c: c.get("masked_id", "").lower() != mid)
    return json.dumps({"matches": matches[:10]}, ensure_ascii=False)


@tool(
    name="zone_check",
    description=(
        "Check whether a village/panchayat is inside the officially notified "
        "zone for a referenced disaster. Returns zone_valid and the notified list."
    ),
)
def zone_check(village: str, panchayat: str, district: str, disaster_ref: str) -> str:
    zones: list[dict[str, Any]] = json.loads(
        (DATA_DIR / "notified_zones.json").read_text(encoding="utf-8")
    )
    ref = disaster_ref.strip().lower()
    for z in zones:
        aliases = [z["disaster_ref"].lower()] + [a.lower() for a in z.get("aliases", [])]
        if ref in aliases or any(ref in a for a in aliases):
            names = {n.lower() for n in z["notified_villages"] + z.get("notified_panchayats", [])}
            valid = village.strip().lower() in names or panchayat.strip().lower() in names
            return json.dumps(
                {
                    "disaster_found": True,
                    "zone_valid": valid,
                    "disaster_ref": z["disaster_ref"],
                    "district": z["district"],
                    "checked_against": sorted(names),
                }
            )
    return json.dumps(
        {
            "disaster_found": False,
            "zone_valid": False,
            "note": f"No notified disaster matching '{disaster_ref}' in the register.",
        }
    )


# ---------------------------------------------------------------------------
# Deterministic backstop — CODE, not a model. Runs after the officer decides.
# "An LLM guardrail is a preference; this check is a control."
# ---------------------------------------------------------------------------

_APPROVAL_RECS = {"recommend_approval", "recommend_partial"}


def _num(x: Any) -> float | None:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        m = re.search(r"[\d,]+(?:\.\d+)?", x)
        if m:
            return float(m.group(0).replace(",", ""))
    return None


def validate_recommendation(
    pack: dict[str, Any],
    norms_finding: dict[str, Any],
    evidence_finding: dict[str, Any],
    integrity_finding: dict[str, Any],
) -> tuple[bool, list[str], float]:
    """Re-derive the maximum defensible amount from the raw specialist outputs
    and check the adjudicator (and any officer modification) against it.

    Returns (ok, violations, max_cited_amount)."""
    violations: list[str] = []

    cited_total = 0.0
    for item in norms_finding.get("items", []):
        amount = _num(item.get("entitlement_amount"))
        citation = item.get("citation") or {}
        cited = bool(citation.get("doc_title")) and bool(citation.get("quoted_clause"))
        if item.get("eligible") == "yes":
            if not cited:
                violations.append(f"Norms item '{item.get('item')}' marked eligible without a citation.")
            elif amount is not None:
                cited_total += amount
        if amount is not None and not cited:
            violations.append(f"Norms item '{item.get('item')}' carries an amount with no citation.")

    rec = pack.get("recommendation", "")
    rec_amount = _num(pack.get("recommended_amount")) or 0.0

    if rec_amount > cited_total + 0.01:
        violations.append(
            f"Recommended amount ₹{rec_amount:,.0f} exceeds the cited-entitlement ceiling ₹{cited_total:,.0f}."
        )

    if rec in _APPROVAL_RECS:
        if evidence_finding.get("overall_consistency") in ("inconsistent", "indeterminate"):
            violations.append("Approval recommended despite inconsistent/indeterminate photographic evidence.")
        if not integrity_finding.get("zone_valid", False):
            violations.append("Approval recommended for a claim outside the notified disaster zone.")
        if (integrity_finding.get("integrity_score") or 0) < 70:
            violations.append(
                f"Approval recommended with integrity score {integrity_finding.get('integrity_score')} (<70)."
            )
        if any(i.get("eligible") == "indeterminate" for i in norms_finding.get("items", [])):
            violations.append("Approval recommended while a norms item is indeterminate.")

    return (len(violations) == 0, violations, cited_total)
