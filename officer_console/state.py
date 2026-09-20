"""Shared state between the workflow driver and the web officer console.

The workflow driver (web_demo.py) and the FastAPI app run in the SAME asyncio
event loop, so this is just a module-level mailbox — no queue, no polling
needed on the Python side. The browser polls /api/pending, which is the only
actual polling in the system, and is normal for a review-queue UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from typing import Any, Optional


class OfficerMailbox:
    def __init__(self) -> None:
        self.pending: Optional[dict[str, Any]] = None  # what the browser sees
        self._decision_event: Optional[asyncio.Event] = None
        self._decision_payload: Optional[dict[str, Any]] = None

    def post_pack(
        self, case_id: str, claim: dict, pack: dict,
        evidence: dict, norms: dict, integrity: dict,
    ) -> None:
        self.pending = {
            "case_id": case_id, "claim": claim, "pack": pack,
            "evidence": evidence, "norms": norms, "integrity": integrity,
        }
        self._decision_event = asyncio.Event()
        self._decision_payload = None

    async def wait_for_decision(self) -> dict[str, Any]:
        assert self._decision_event is not None, "post_pack() must be called first"
        await self._decision_event.wait()
        return self._decision_payload  # type: ignore[return-value]

    def submit_decision(self, payload: dict[str, Any]) -> bool:
        """Cleared here, not in wait_for_decision(): the HTTP caller gets an
        immediate, consistent view (no stale 'pending' between decide and the
        workflow driver picking it up), and a duplicate submit is still
        rejected via the event's is_set() guard below."""
        if self._decision_event is None or self._decision_event.is_set():
            return False
        self._decision_payload = payload
        self.pending = None
        self._decision_event.set()
        return True


mailbox = OfficerMailbox()


def dc_to_dict(obj: Any) -> dict:
    return asdict(obj) if is_dataclass(obj) else obj
