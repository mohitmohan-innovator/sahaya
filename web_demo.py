"""SAHAYA — web-driven run: identical orchestration to workflow.py, but the
Village Officer decides through the browser console instead of a terminal.

The FastAPI app (officer_console/server.py) and this driver share ONE asyncio
event loop — started together below — so posting a decision in the browser
resumes the real workflow run directly. There is no mock in this path: same
agents, same fan-out/fan-in, same backstop, same audit record.

Run:
    az login
    python web_demo.py data/sample_claims/claim_clean_en.txt --photos data/photos/house1.jpg
Then open http://127.0.0.1:8000
"""

import argparse
import asyncio
import json
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from rich.console import Console

from officer_console.server import app
from officer_console.state import mailbox
from workflow import (
    ClaimSubmission,
    OfficerDecision,
    build_agents,
    build_workflow,
)

load_dotenv()
console = Console()
OUT_DIR = Path(__file__).parent / "out"


async def run_claim(wf, submission: ClaimSubmission) -> None:
    console.print("[dim]Running: intake \u2192 (evidence \u2225 norms \u2225 integrity) \u2192 adjudicate\u2026[/dim]")
    result = await wf.run(submission)

    pending = result.get_request_info_events()
    while pending:
        responses = {}
        for ev in pending:
            req = ev.data  # OfficerRequest
            console.print(f"[cyan]Case {req.case_id} is awaiting the officer in the browser\u2026[/cyan]")
            mailbox.post_pack(
                case_id=req.case_id,
                claim=json.loads(req.claim_json),
                pack=json.loads(req.pack_json),
                evidence=json.loads(req.evidence_json),
                norms=json.loads(req.norms_json),
                integrity=json.loads(req.integrity_json),
            )
            decision_payload = await mailbox.wait_for_decision()
            console.print(f"[green]Officer decision received:[/green] {decision_payload['decision']}")
            responses[ev.request_id] = OfficerDecision(
                decision=decision_payload["decision"],
                comments=decision_payload.get("comments", ""),
                modified_amount=decision_payload.get("modified_amount"),
                officer_name=decision_payload.get("officer_name", "Village Officer (demo)"),
            )
        result = await wf.run(responses=responses)
        pending = result.get_request_info_events()

    for out in result.get_outputs():
        if isinstance(out, dict) and out.get("final_status") == "needs_information":
            console.print(f"[yellow]Intake needs more detail:[/yellow] {out['question']}")
            continue
        console.print(
            f"[bold]Final status:[/bold] {out.get('final_status')}  "
            f"[bold]Amount:[/bold] \u20b9{out.get('approved_amount', 0):,}  "
            f"[dim]\u2014 out/{out.get('case_id')}.decision.json[/dim]"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="SAHAYA web-driven demo")
    parser.add_argument("claim_file")
    parser.add_argument("--photos", nargs="*", default=[])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # give uvicorn a moment to bind before we open the tab / start the workflow
    await asyncio.sleep(0.6)
    url = f"http://127.0.0.1:{args.port}"
    console.print(f"[bold]Officer Console:[/bold] {url}")
    if not args.no_browser:
        webbrowser.open(url)
    import os
    from azure.monitor.opentelemetry import configure_azure_monitor
    from agent_framework.observability import enable_sensitive_telemetry
    configure_azure_monitor(connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"])
    enable_sensitive_telemetry()
    agents = build_agents()
    wf = build_workflow(agents)
    claim_text = Path(args.claim_file).read_text(encoding="utf-8")
    submission = ClaimSubmission(text=claim_text, photo_paths=args.photos)

    await run_claim(wf, submission)

    console.print("[dim]Run complete. Ctrl+C to stop the console server.[/dim]")
    await server_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
