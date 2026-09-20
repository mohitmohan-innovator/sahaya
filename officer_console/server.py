"""Officer Console API — serves the pending Officer Pack and accepts decisions.

Run standalone for UI development:  uvicorn officer_console.server:app --reload
In the real demo it's started programmatically by web_demo.py, in the same
event loop as the workflow, so a decision posted here resumes the workflow
run directly — this is not a mock.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .state import mailbox

app = FastAPI(title="SAHAYA Officer Console")

STATIC_DIR = Path(__file__).parent / "static"


class DecisionIn(BaseModel):
    case_id: str
    decision: str  # "approve" | "modify" | "reject"
    comments: str = ""
    modified_amount: float | None = None
    officer_name: str = "Village Officer (demo)"


@app.get("/api/pending")
def get_pending():
    """Polled by the browser every 1.5s. Null when no case awaits a decision."""
    return mailbox.pending


@app.post("/api/decide")
def post_decision(body: DecisionIn):
    if mailbox.pending is None or mailbox.pending["case_id"] != body.case_id:
        raise HTTPException(409, "This case is no longer pending (already decided, or stale).")
    ok = mailbox.submit_decision(body.model_dump())
    if not ok:
        raise HTTPException(409, "A decision was already submitted for this case.")
    return {"status": "accepted"}


class SeedIn(BaseModel):
    case_id: str
    claim: dict
    pack: dict
    evidence: dict = {}
    norms: dict = {}
    integrity: dict = {}


@app.post("/api/_dev_seed")
def dev_seed(body: SeedIn):
    """Dev/demo utility only \u2014 not part of the workflow contract. Lets you
    load a sample case into the console for a UI rehearsal or as a fallback
    if live model calls are slow while presenting. Never called by web_demo.py."""
    mailbox.post_pack(
        case_id=body.case_id, claim=body.claim, pack=body.pack,
        evidence=body.evidence, norms=body.norms, integrity=body.integrity,
    )
    return {"status": "seeded"}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
