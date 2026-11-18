# SAHAYA — Multi-Agent Disaster Relief Claim Adjudication

*Every rupee cited. Every decision human.*

> **Disclaimer.** SAHAYA is a reference sample, not a government system. All
> claimants, IDs and claim records in `data/` are synthetic. `notified_zones.json`
> is illustrative, not official notification data. The government norms PDFs
> are **not** included; you download them yourself (see `norms/README.md`).
> Do not use this sample to decide real relief claims.

## Deploy with Azure Developer CLI (azd)

This is an **infrastructure template**: `azd up` provisions the Azure resources,
and the agents and Officer Console run locally from this repo.

**Provisions:** Microsoft Foundry resource and project, a gpt-4o deployment
(vision-capable, for the five agents), a text-embedding-3-large deployment,
Azure AI Search (Basic), Log Analytics and Application Insights (connected to the
Foundry project for tracing), and the Foundry User role for the person deploying.

**Prerequisites:** an Azure subscription with access to Microsoft Foundry and
model quota for both deployments in your chosen region, the Azure Developer CLI,
the Azure CLI, Python 3.11+, and PowerShell 7 on Windows.

```bash
azd auth login
az login
azd up            # choose a subscription and a region with both models available
```

After provisioning, a hook writes your local `.env`. Then:

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# put the SDRF norms + Kerala G.O. PDFs in ./norms/
python build_index.py
python web_demo.py data/sample_claims/claim_clean_en.txt
```

**Cost.** AI Search Basic is billed for every hour it exists; model calls,
Application Insights and Log Analytics are billed by usage. Check the
[Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/)
for your region, and delete everything when you are done:

```bash
azd down --purge
```

### Known limitations
- Infrastructure-only: the app runs locally; it is not deployed to a hosting service.
- Azure AI Search and the embedding client still use API keys (written only to
  your local, gitignored `.env`). The five agents use Entra ID. Moving both to
  keyless access is on the roadmap.
- The Officer Console is a single-officer local UI with no sign-in.
- The integrity-score threshold (70) is a reasoned default, not calibrated on
  labeled data.

Five specialist agents on Microsoft Foundry, orchestrated deterministically by
the Microsoft Agent Framework, turn a citizen's disaster relief claim
(English or Malayalam, text + damage photos) into an evidence-checked,
norm-cited recommendation pack — which a Village Officer approves, modifies or
rejects at a human gate. A deterministic code backstop then re-validates the
arithmetic against the raw specialist outputs before anything is recorded.

```
Intake → (Evidence ∥ Norms ∥ Integrity) → Adjudicator → Officer gate → Backstop → Decision record
```

Built for the Microsoft Agent-a-thon, Level 3 Architect. Context: Kerala's
post-disaster relief adjudication (2018 floods, 2024 Wayanad landslides),
where verification and entitlement calculation — not payment rails — are the
bottleneck. All claimants and IDs in this repository are synthetic.

## Design principles

1. **SAHAYA never approves.** It recommends; the officer decides. The final
   `key_finding` of every pack is "Pending human adjudication by the Village
   Officer."
2. **No uncited rupee.** Entitlement amounts come only from passages retrieved
   from the indexed SDRF norms and Kerala G.O.s, with document + page + quoted
   clause. No amount is hardcoded anywhere in this codebase.
3. **An LLM guardrail is a preference; a code check is a control.** After the
   officer decides, `tools.validate_recommendation` re-derives the maximum
   defensible amount from the raw specialist JSON and blocks anything above it
   — including officer modifications.
4. **Deterministic process, probabilistic reasoning.** The workflow graph is
   code; only what happens inside each agent is a model.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
az login
cp .env.example .env    # fill in endpoints and deployment names
```

Azure prerequisites (see the build guide): a Foundry project with a
multimodal chat deployment + text-embedding-3-large, an AI Search service,
and the norms PDFs downloaded into `./norms` (see `norms/README.md`).

```bash
python build_index.py                      # chunk, embed, index the norms
python workflow.py --viz data/sample_claims/claim_clean_en.txt   # export graph only
python workflow.py data/sample_claims/claim_clean_en.txt --photos data/photos/house1.jpg
python workflow.py data/sample_claims/claim_ml.txt               # Malayalam intake
python workflow.py data/sample_claims/claim_injection.txt        # watch it refuse
python -m eval.run_evals                   # the 5-case safety suite (live inference)
```

Decision records land in `out/<CASE>.decision.json` — the full audit trail:
claim, all three specialist findings, the pack, the officer's decision, and
the backstop verdict.

## Officer Console (web UI)

`workflow.py`'s console loop is a developer tool. For anything demo- or
user-facing, use `web_demo.py` instead — same orchestration, same backstop,
same audit record, but the Village Officer decides through a browser screen
rather than typing `a`/`m`/`r` at a terminal:

```bash
python web_demo.py data/sample_claims/claim_clean_en.txt --photos data/photos/house1.jpg
```

This opens `http://127.0.0.1:8000`. The workflow genuinely pauses at the
human gate — `officer_console/state.py` is a one-slot mailbox shared between
the FastAPI app and the workflow driver in the same asyncio event loop, so a
decision submitted in the browser resumes the real `agent_framework` run.
Nothing here is mocked: the citations, the amounts, the backstop check on
submit — all the same code paths as `workflow.py`.

**Rehearsing the UI without live model calls.** `POST /api/_dev_seed` loads a
sample case straight into the console (see `officer_console/server.py` for
the schema) — useful for a UI walkthrough, or as a fallback if inference is
slow while presenting. It is never called by `web_demo.py` itself.

**Production note:** this is still a challenge-scope surface (FastAPI +
static HTML, run locally). The interaction pattern — server-side pending
state, a poll-and-decide contract — maps directly onto a Teams Adaptive Card
or a Power Apps screen; the JSON contract (`OfficerRequest` in and
`OfficerDecision` out) doesn't change either way.

## Repository map

| Path | What it is |
|---|---|
| `workflow.py` | Orchestration: executors, fan-out/fan-in, officer gate, console loop |
| `instructions.py` | The five agent system prompts (versioned; v1.0) |
| `tools.py` | `search_norms`, `registry_lookup`, `zone_check` + the deterministic backstop |
| `build_index.py` | Chunks and embeds the norms PDFs into Azure AI Search |
| `data/` | Synthetic claims registry, notified zones, five sample claims |
| `eval/` | Gold set + harness asserting P1–P5 safety properties |
| `out/` | Decision records, workflow graph (mermaid/SVG) |

## Version pinning

Written and API-verified against `agent-framework==1.18.0`
(`agent-framework-foundry==1.13.0`). The framework moves fast; if an import
fails on a newer version, install the pinned versions or check the current
samples at learn.microsoft.com/agent-framework.

## Observability

Foundry-hosted model calls appear in the project's tracing view. To export
Agent Framework spans to Application Insights, wire OpenTelemetry per the
framework's observability docs (`agent_framework.observability` —
`configure_otel_providers`, `enable_instrumentation`); connect your App
Insights resource to the Foundry project first.

## Prompt changelog

- v1.0 — initial five prompts.

## Known limitations (deliberate scope for the challenge)

- Officer gate is a console loop; production would surface it as a Teams
  approval or a Power Apps queue.
- AI Search uses key auth locally; production posture is managed identity +
  Key Vault throughout.
- The claims registry and notified zones are JSON files standing in for
  Dataverse/DB tables.
- Photo authenticity checks are model-visual only; production would add EXIF
  and perceptual-hash verification in code.

# sahaya