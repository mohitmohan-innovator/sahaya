"""SAHAYA — Multi-Agent Disaster Relief Claim Adjudication.

Architecture: Intake → (Evidence ∥ Norms ∥ Integrity) → Adjudicator →
Village-Officer gate (human-in-the-loop) → deterministic backstop → decision
record. Orchestration is deterministic (this file); only the reasoning inside
each step is probabilistic (Foundry-hosted models).

Run:
    az login
    python workflow.py data/sample_claims/claim_clean_en.txt --photos data/photos/house1.jpg
    python workflow.py data/sample_claims/claim_injection.txt

Verified against agent-framework==1.18.0 / agent-framework-foundry==1.13.0.
If a later version renames an API, `pip install agent-framework==1.18.0` or
check the current samples at learn.microsoft.com/agent-framework.

NOTE: no `from __future__ import annotations` here — the framework's
@handler/@response_handler validators resolve annotations at runtime and
reject stringified ones.
"""

import argparse
import asyncio
import base64
import json
import mimetypes
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent_framework import (
    Agent,
    Content,
    Executor,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowViz,
    executor,
    handler,
    response_handler,
)
from azure.identity import AzureCliCredential

import instructions as ins
from tools import registry_lookup, search_norms, validate_recommendation, zone_check

load_dotenv()
console = Console()
OUT_DIR = Path(__file__).parent / "out"
OUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Messages that flow along workflow edges
# ---------------------------------------------------------------------------

@dataclass
class ClaimSubmission:
    text: str
    photo_paths: list[str] = field(default_factory=list)


@dataclass
class StructuredClaim:
    claim_json: str
    photo_paths: list[str] = field(default_factory=list)


@dataclass
class SpecialistFinding:
    kind: str  # "evidence" | "norms" | "integrity"
    claim_json: str
    finding_json: str


@dataclass
class AdjudicationPack:
    claim_json: str
    pack_json: str
    evidence_json: str
    norms_json: str
    integrity_json: str


@dataclass
class OfficerRequest:
    """What the human sees at the gate."""

    case_id: str
    claim_json: str
    pack_json: str
    evidence_json: str = "{}"
    norms_json: str = "{}"
    integrity_json: str = "{}"


@dataclass
class OfficerDecision:
    decision: str  # "approve" | "modify" | "reject"
    comments: str = ""
    modified_amount: float | None = None
    officer_name: str = "Village Officer (demo)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of a model reply."""
    cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in model output: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(cleaned[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : i + 1])
    raise ValueError("Unbalanced JSON in model output.")


def photo_message(claim_json: str, photo_paths: list[str]) -> Message:
    contents: list[Content] = [
        Content.from_text(
            "Assess the attached photograph(s) against this claim.\n\nCLAIM:\n" + claim_json
        )
    ]
    for p in photo_paths:
        data = Path(p).read_bytes()
        media = mimetypes.guess_type(p)[0] or "image/jpeg"
        contents.append(Content.from_data(data=data, media_type=media))
    return Message(role="user", contents=contents)

_APPROVAL_RECS = ("recommend_approval", "recommend_partial")


def enforce_pre_gate(pack: dict, by_kind: dict) -> dict:
    """Run the deterministic validator BEFORE the officer sees the pack.
    A failing pack is never shown as an approval: it is held at field
    verification, limited to the supported ceiling, and the failures are
    listed for the officer."""
    ok, violations, ceiling = validate_recommendation(
        pack,
        norms_finding=json.loads(by_kind.get("norms", "{}")),
        evidence_finding=json.loads(by_kind.get("evidence", "{}")),
        integrity_finding=json.loads(by_kind.get("integrity", "{}")),
    )
    if ok:
        return pack
    original = {
        "recommendation": pack.get("recommendation"),
        "recommended_amount": pack.get("recommended_amount"),
    }
    if pack.get("recommendation") in _APPROVAL_RECS:
        pack["recommendation"] = "recommend_field_verification"
    try:
        amount = float(pack.get("recommended_amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    limited = min(amount, ceiling)
    pack["recommended_amount"] = int(limited) if limited == int(limited) else limited
    pack["open_questions"] = list(pack.get("open_questions", [])) + [
        f"Automated check: {v}" for v in violations
    ]
    note = (
        f"Backstop: {len(violations)} automated check(s) failed before review; "
        "recommendation held at field verification and the amount limited to the supported ceiling."
    )
    kf = list(pack.get("key_findings", []))
    pending = "Pending human adjudication by the Village Officer."
    if kf and kf[-1] == pending:
        kf.insert(len(kf) - 1, note)
    else:
        kf.append(note)
    pack["key_findings"] = kf
    pack["pre_gate_check"] = {"ok": False, "violations": violations, "original": original}
    return pack
# ---------------------------------------------------------------------------
# Agents (created once, injected into executors)
# ---------------------------------------------------------------------------

def build_agents() -> dict[str, Agent]:
    import os

    from agent_framework.foundry import FoundryChatClient

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["FOUNDRY_MODEL"],
        credential=AzureCliCredential(),
    )
    return {
        "intake": client.as_agent(name="sahaya-intake", instructions=ins.INTAKE_INSTRUCTIONS),
        "evidence": client.as_agent(name="sahaya-evidence", instructions=ins.EVIDENCE_INSTRUCTIONS),
        "norms": client.as_agent(
            name="sahaya-norms", instructions=ins.NORMS_INSTRUCTIONS, tools=[search_norms]
        ),
        "integrity": client.as_agent(
            name="sahaya-integrity",
            instructions=ins.INTEGRITY_INSTRUCTIONS,
            tools=[registry_lookup, zone_check],
        ),
        "adjudicator": client.as_agent(
            name="sahaya-adjudicator", instructions=ins.ADJUDICATOR_INSTRUCTIONS
        ),
    }


# ---------------------------------------------------------------------------
# The Village Officer gate — pauses the workflow for a human decision, then
# applies the deterministic backstop and yields the final decision record.
# ---------------------------------------------------------------------------

class OfficerGate(Executor):
    def __init__(self) -> None:
        super().__init__(id="village_officer_gate")
        self._pending: dict[str, AdjudicationPack] = {}

    @handler
    async def on_pack(self, pack: AdjudicationPack, ctx: WorkflowContext) -> None:
        case_id = f"SAH-{uuid.uuid4().hex[:6].upper()}"
        self._pending[case_id] = pack
        await ctx.request_info(
            OfficerRequest(
                case_id=case_id,
                claim_json=pack.claim_json,
                pack_json=pack.pack_json,
                evidence_json=pack.evidence_json,
                norms_json=pack.norms_json,
                integrity_json=pack.integrity_json,
            ),
            OfficerDecision,
            request_id=case_id,
        )

    @response_handler
    async def on_decision(
        self, request: OfficerRequest, decision: OfficerDecision, ctx: WorkflowContext
    ) -> None:
        pack = self._pending.pop(request.case_id)
        pack_obj = json.loads(pack.pack_json)

        # Officer modification overrides the recommended amount — the backstop
        # still applies: no human or model may exceed the cited ceiling.
        if decision.decision == "modify" and decision.modified_amount is not None:
            pack_obj["recommended_amount"] = decision.modified_amount

        ok, violations, ceiling = validate_recommendation(
            pack_obj,
            norms_finding=json.loads(pack.norms_json),
            evidence_finding=json.loads(pack.evidence_json),
            integrity_finding=json.loads(pack.integrity_json),
        )

        approved = decision.decision in ("approve", "modify") and ok
        if decision.decision in ("approve", "modify") and not ok:
            # The control fires: force field verification, never silent approval.
            final_status = "blocked_by_backstop"
        elif decision.decision == "reject":
            final_status = "rejected"
        else:
            final_status = "approved"

        record = {
            "case_id": request.case_id,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "officer": decision.officer_name,
            "officer_decision": decision.decision,
            "officer_comments": decision.comments,
            "final_status": final_status,
            "approved_amount": pack_obj.get("recommended_amount") if approved else 0,
            "cited_ceiling": ceiling,
            "backstop_ok": ok,
            "backstop_violations": violations,
            "claim": json.loads(pack.claim_json),
            "recommendation_pack": pack_obj,
            "specialist_findings": {
                "evidence": json.loads(pack.evidence_json),
                "norms": json.loads(pack.norms_json),
                "integrity": json.loads(pack.integrity_json),
            },
        }
        audit_path = OUT_DIR / f"{request.case_id}.decision.json"
        audit_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        await ctx.yield_output(record)


# ---------------------------------------------------------------------------
# Workflow assembly
# ---------------------------------------------------------------------------
def _is_content_filter_block(exc: Exception) -> bool:
    """Azure's platform-level content/jailbreak filter blocked the prompt
    before the model ever ran. agent_framework_openai has a library bug
    (as of this build) where it crashes with a ValueError while trying to
    *describe* this exact case, instead of raising its own
    OpenAIContentFilterException cleanly. Catch both shapes and treat it as
    the safety layer working correctly, not a failure."""
    msg = str(exc)
    return (
        "content_filter" in msg
        or "ContentFiltered" in msg
        or "jailbreak" in msg.lower()
        or type(exc).__name__ == "OpenAIContentFilterException"
    )
def build_workflow(agents: dict[str, Agent]):
    @executor(id="intake")
    async def intake(claim: ClaimSubmission, ctx: WorkflowContext[StructuredClaim, dict]) -> None:
        try:
            reply = await agents["intake"].run(claim.text)
        except Exception as exc:
            if _is_content_filter_block(exc):
                await ctx.yield_output({
                    "final_status": "blocked_by_platform_filter",
                    "case_id": "N/A",
                    "note": (
                        "Blocked by Azure OpenAI's content safety layer (jailbreak "
                        "detection) before reaching SAHAYA's own agents -- the "
                        "outermost layer of the same defense-in-depth the Intake "
                        "agent's own flagging and the deterministic backstop provide."
                    ),
                })
                return
            raise
        data = extract_json(reply.text)
        if data.get("status") == "incomplete":
            await ctx.yield_output(
                {"final_status": "needs_information", "question": data.get("question", "")}
            )
            return
        await ctx.send_message(
            StructuredClaim(claim_json=json.dumps(data, ensure_ascii=False), photo_paths=claim.photo_paths)
        )

    @executor(id="evidence")
    async def evidence(claim: StructuredClaim, ctx: WorkflowContext[SpecialistFinding]) -> None:
        if claim.photo_paths:
            reply = await agents["evidence"].run(photo_message(claim.claim_json, claim.photo_paths))
            finding = extract_json(reply.text)
        else:
            finding = {"photos": [], "overall_consistency": "indeterminate", "authenticity_flags": []}
        await ctx.send_message(
            SpecialistFinding("evidence", claim.claim_json, json.dumps(finding, ensure_ascii=False))
        )

    @executor(id="norms")
    async def norms(claim: StructuredClaim, ctx: WorkflowContext[SpecialistFinding]) -> None:
        reply = await agents["norms"].run(
            "Determine entitlements for this claim using search_norms only.\n\nCLAIM:\n"
            + claim.claim_json
        )
        finding = extract_json(reply.text)
        await ctx.send_message(
            SpecialistFinding("norms", claim.claim_json, json.dumps(finding, ensure_ascii=False))
        )

    @executor(id="integrity")
    async def integrity(claim: StructuredClaim, ctx: WorkflowContext[SpecialistFinding]) -> None:
        reply = await agents["integrity"].run(
            "Screen this claim. Use registry_lookup and zone_check.\n\nCLAIM:\n" + claim.claim_json
        )
        finding = extract_json(reply.text)
        await ctx.send_message(
            SpecialistFinding("integrity", claim.claim_json, json.dumps(finding, ensure_ascii=False))
        )

    @executor(id="adjudicate")
    async def adjudicate(findings: list[SpecialistFinding], ctx: WorkflowContext[AdjudicationPack]) -> None:
        by_kind = {f.kind: f.finding_json for f in findings}
        claim_json = findings[0].claim_json
        prompt = (
            "Produce the Officer Recommendation Pack.\n\n"
            f"CLAIM:\n{claim_json}\n\n"
            f"EVIDENCE FINDING:\n{by_kind.get('evidence', '{}')}\n\n"
            f"NORMS FINDING:\n{by_kind.get('norms', '{}')}\n\n"
            f"INTEGRITY FINDING:\n{by_kind.get('integrity', '{}')}"
        )
        reply = await agents["adjudicator"].run(prompt)
        pack = extract_json(reply.text)
        pack = enforce_pre_gate(pack, by_kind)
        await ctx.send_message(
            AdjudicationPack(
                claim_json=claim_json,
                pack_json=json.dumps(pack, ensure_ascii=False),
                evidence_json=by_kind.get("evidence", "{}"),
                norms_json=by_kind.get("norms", "{}"),
                integrity_json=by_kind.get("integrity", "{}"),
            )
        )

    gate = OfficerGate()

    wf = (
        WorkflowBuilder(start_executor=intake, name="SAHAYA")
        .add_fan_out_edges(intake, [evidence, norms, integrity])
        .add_fan_in_edges([evidence, norms, integrity], adjudicate)
        .add_edge(adjudicate, gate)
        .build()
    )
    return wf


# ---------------------------------------------------------------------------
# Console: run, pause at the officer gate, resume with the decision
# ---------------------------------------------------------------------------

def show_pack(req: OfficerRequest) -> None:
    claim = json.loads(req.claim_json)
    pack = json.loads(req.pack_json)
    t = Table(title=f"Officer Recommendation Pack — {req.case_id}", show_lines=False)
    t.add_column("Field", style="bold")
    t.add_column("Value", overflow="fold")
    t.add_row("Claimant", f"{claim.get('claimant_name')} · {claim.get('village')}, {claim.get('district')}")
    t.add_row("Disaster", claim.get("disaster_ref", ""))
    t.add_row("Category", claim.get("damage_category", ""))
    t.add_row("Recommendation", f"[bold]{pack.get('recommendation')}[/bold]")
    t.add_row("Recommended amount", f"₹{pack.get('recommended_amount', 0):,}")
    t.add_row("Confidence", str(pack.get("confidence")))
    for kf in pack.get("key_findings", []):
        t.add_row("Finding", kf)
    for q in pack.get("open_questions", []):
        t.add_row("Open question", f"[yellow]{q}[/yellow]")
    t.add_row("Citizen msg (EN)", pack.get("citizen_message_en", ""))
    t.add_row("Citizen msg (ML)", pack.get("citizen_message_ml", ""))
    console.print(t)


def ask_officer() -> OfficerDecision:
    while True:
        raw = console.input("[bold cyan]Decision — (a)pprove / (m)odify / (r)eject: [/bold cyan]").strip().lower()
        if raw in ("a", "approve"):
            comments = console.input("Comments: ").strip()
            return OfficerDecision("approve", comments)
        if raw in ("r", "reject"):
            comments = console.input("Reason (recorded): ").strip()
            return OfficerDecision("reject", comments)
        if raw in ("m", "modify"):
            amt = console.input("Approved amount (INR, numbers only): ").strip()
            comments = console.input("Comments: ").strip()
            try:
                return OfficerDecision("modify", comments, modified_amount=float(amt))
            except ValueError:
                console.print("[red]Enter a number.[/red]")
        else:
            console.print("[red]a, m or r.[/red]")


async def main() -> None:
    parser = argparse.ArgumentParser(description="SAHAYA claim adjudication")
    parser.add_argument("claim_file", help="Text file containing the citizen's claim")
    parser.add_argument("--photos", nargs="*", default=[], help="Damage photo paths")
    parser.add_argument("--viz", action="store_true", help="Export the workflow graph and exit")
    args = parser.parse_args()

    console.print(Panel.fit("[bold]SAHAYA[/bold] — every rupee cited · every decision human", style="green"))
    import os
    from azure.monitor.opentelemetry import configure_azure_monitor
    from agent_framework.observability import enable_sensitive_telemetry
    configure_azure_monitor(connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"])
    enable_sensitive_telemetry()
    agents = build_agents()
    wf = build_workflow(agents)

    viz = WorkflowViz(wf)
    (OUT_DIR / "workflow.mmd").write_text(viz.to_mermaid(), encoding="utf-8")
    try:
        viz.save_svg(str(OUT_DIR / "workflow.svg"))  # needs graphviz binaries
    except Exception:
        pass
    if args.viz:
        console.print(f"Graph exported to {OUT_DIR}/workflow.mmd")
        return

    claim_text = Path(args.claim_file).read_text(encoding="utf-8")
    submission = ClaimSubmission(text=claim_text, photo_paths=args.photos)

    console.print("[dim]Running: intake → (evidence ∥ norms ∥ integrity) → adjudicate…[/dim]")
    result = await wf.run(submission)

    # Human-in-the-loop: the run pauses whenever the gate raises a request.
    pending = result.get_request_info_events()
    while pending:
        responses: dict[str, OfficerDecision] = {}
        for ev in pending:
            show_pack(ev.data)  # ev.data is the OfficerRequest
            responses[ev.request_id] = ask_officer()
        console.print("[dim]Resuming workflow with the officer's decision…[/dim]")
        result = await wf.run(responses=responses)
        pending = result.get_request_info_events()

    for out in result.get_outputs():
        if isinstance(out, dict) and out.get("final_status") == "needs_information":
            console.print(Panel(out["question"], title="Intake needs one more detail", style="yellow"))
            continue
        if isinstance(out, dict) and out.get("final_status") == "blocked_by_platform_filter":
            console.print(Panel(out["note"], title="Blocked at the platform layer", style="cyan"))
            continue
        style = {"approved": "green", "rejected": "red"}.get(out.get("final_status", ""), "yellow")
        console.print(
            Panel(
                f"Status: [bold]{out['final_status']}[/bold]\n"
                f"Approved amount: ₹{out.get('approved_amount', 0):,}  (cited ceiling ₹{out.get('cited_ceiling', 0):,.0f})\n"
                f"Backstop: {'clean' if out.get('backstop_ok') else 'VIOLATIONS — ' + '; '.join(out.get('backstop_violations', []))}\n"
                f"Audit record: out/{out['case_id']}.decision.json",
                title=f"Final decision — {out.get('case_id', '')}",
                style=style,
            )
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
