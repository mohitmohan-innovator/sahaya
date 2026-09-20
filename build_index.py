"""Build the sahaya-norms Azure AI Search index from PDFs in ./norms.

Put the real documents there first (see norms/README.md):
  - Items and Norms of Assistance from SDRF and NDRF (MHA, latest revision)
  - 2-4 Kerala Revenue/DM department G.O.s on relief assistance

Run:  az login && python build_index.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from dotenv import load_dotenv
from pypdf import PdfReader

load_dotenv(override=True)
DIM = 3072  # text-embedding-3-large
INDEX = "sahaya-norms"
NORMS_DIR = Path(__file__).parent / "norms"


def make_index() -> None:
    client = SearchIndexClient(
        os.environ["SEARCH_ENDPOINT"], AzureKeyCredential(os.environ["SEARCH_KEY"])
    )
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="breadcrumb", type=SearchFieldDataType.String),
        SimpleField(name="doc_title", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="clause_hint", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
        SearchField(
            name="vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=DIM,
            vector_search_profile_name="hnsw",
        ),
    ]
    vs = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name="alg")],
        profiles=[VectorSearchProfile(name="hnsw", algorithm_configuration_name="alg")],
    )
    sem = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name="sem",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="breadcrumb"),
                    content_fields=[SemanticField(field_name="content")],
                ),
            )
        ]
    )
    client.create_or_update_index(
        SearchIndex(name=INDEX, fields=fields, vector_search=vs, semantic_search=sem)
    )
    print(f"Index '{INDEX}' ready.")


def chunks_from_pdf(path: Path):
    """Norms documents are table-heavy: chunk generously by page paragraphs so
    an amount is never separated from the item it applies to."""
    reader = PdfReader(str(path))
    title = path.name
    for pno, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        for para in re.split(r"\n{2,}", text):
            para = re.sub(r"[ \t]+", " ", para).strip()
            if len(para) < 80:
                continue
            m = re.search(r"\b(\d+(?:\.\d+)*[.)]?|\([a-z]\))", para)
            yield {
                "id": hashlib.md5(f"{title}-{pno}-{para[:60]}".encode()).hexdigest(),
                "content": para[:4000],
                "breadcrumb": f"{title} | page {pno}",
                "doc_title": title,
                "clause_hint": m.group(0) if m else "",
                "page": pno,
            }


async def main() -> None:
    make_index()

    from openai import AsyncAzureOpenAI

    embedder = AsyncAzureOpenAI(
        api_key=os.environ["FOUNDRY_MODELS_API_KEY"],
        azure_endpoint=os.environ["FOUNDRY_MODELS_ENDPOINT"],
        api_version="2023-05-15",
    )
    sc = SearchClient(
        os.environ["SEARCH_ENDPOINT"], INDEX, AzureKeyCredential(os.environ["SEARCH_KEY"])
    )
    print(f"Embedding via: {os.environ['FOUNDRY_MODELS_ENDPOINT']!r}  deployment: {os.environ['FOUNDRY_EMBEDDING_MODEL']!r}")

    pdfs = sorted(NORMS_DIR.glob("*.pdf"))
    if not pdfs:
        raise SystemExit("No PDFs in ./norms — download the SDRF norms and Kerala G.O.s first.")

    docs = [c for pdf in pdfs for c in chunks_from_pdf(pdf)]
    print(f"{len(docs)} chunks from {len(pdfs)} document(s). Embedding + uploading…")

    BATCH = 16
    for i in range(0, len(docs), BATCH):
        batch = docs[i : i + BATCH]
        print("ENDPOINT   =", repr(os.environ["FOUNDRY_MODELS_ENDPOINT"]))
        print("DEPLOYMENT =", repr(os.environ["FOUNDRY_EMBEDDING_MODEL"]))
        print("CLIENT BASE =", repr(str(embedder.base_url)))
        result = await embedder.embeddings.create(model=os.environ["FOUNDRY_EMBEDDING_MODEL"],input=[d["content"] for d in batch],)
        for d, e in zip(batch, result.data):
            d["vector"] = e.embedding
            
        sc.upload_documents(batch)
        print(f"  {min(i + BATCH, len(docs))}/{len(docs)}")

    print("Done. Sanity-check in the portal: Search explorer → query 'fully damaged house'.")


if __name__ == "__main__":
    asyncio.run(main())
