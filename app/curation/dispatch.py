"""One dispatcher for the curator tool surface (ADR-0002 §5.5).

The in-process curator loop and the MCP server both call ``dispatch``, so
there is exactly one implementation of every tool. Acceptance: the same
``propose_ops`` call returns byte-identical results through either entry.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import CurationJob, CurationProposal, Document, new_uuid
from . import tools
from .ops import apply_ops, build_context

READ_TOOLS = {
    "get_pages_overview": tools.get_pages_overview,
    "get_page_blocks": tools.get_page_blocks,
    "get_page_text": tools.get_page_text,
    "get_chunks": tools.get_chunks,
    "get_quality": tools.get_quality,
    "get_usage": tools.get_usage,
    "get_toc_nodes": tools.get_toc_nodes,
    "get_printed_toc_llm": tools.get_printed_toc_llm,
}

WRITE_TOOLS = {"propose_ops", "apply_proposal", "get_proposal"}


class DispatchError(ValueError):
    """Bad tool name, bad arguments, exhausted budget or cross-doc call."""


class Budget:
    """Calls/token budget for one document's curator loop.

    ``tokens_left`` is real, not cosmetic: the agent loop reports each turn's
    token usage back through ``spend_tokens`` (ADR-0002 §6.4). A loop that
    keeps making LLM calls burns its remaining tool-call budget too — each
    ``llm_chat`` turn after exhaustion fails ``spend()`` at the next
    ``dispatch``, so the run winds down instead of silently quadrupling the
    wall-clock estimate.
    """

    def __init__(self, calls_left: int = 20, tokens_left: int = 120_000):
        self.calls_left = calls_left
        self.tokens_left = tokens_left

    def spend(self) -> None:
        if self.calls_left <= 0:
            raise DispatchError("budget_exhausted")
        self.calls_left -= 1

    def spend_tokens(self, total: int) -> bool:
        """Deduct one turn's tokens; False once the token budget is gone."""
        self.tokens_left -= max(0, int(total))
        return self.tokens_left > 0

    def state(self) -> dict:
        return {"calls_left": max(0, self.calls_left), "tokens_left": max(0, self.tokens_left)}


class Dispatcher:
    """Bound to one document: cross-document calls are refused (v1)."""

    def __init__(
        self,
        db: AsyncSession,
        data_dir,
        doc_id: str,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        budget: Budget | None = None,
        verify: bool = True,
    ):
        self.db = db
        self.data_dir = data_dir
        self.doc_id = doc_id
        self.job_id = job_id
        self.run_id = run_id
        self.budget = budget or Budget()
        # Closed-loop verification rides on every write (self-evolution L2).
        self.verify = verify

    async def dispatch(self, name: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        requested_doc = args.pop("doc_id", self.doc_id)
        if requested_doc != self.doc_id:
            raise DispatchError("cross-document calls are refused in v1")
        self.budget.spend()
        budget_state = self.budget.state()

        if name in READ_TOOLS:
            result = await READ_TOOLS[name](
                self.db, self.data_dir, self.doc_id, **args, **budget_state
            )
            result["budget"] = budget_state
            return result
        if name == "propose_ops":
            return await self.propose_ops(
                args.get("ops") or [], args.get("rationale") or ""
            )
        if name == "get_proposal":
            return await self.get_proposal(str(args.get("proposal_id") or ""))
        if name == "apply_proposal":
            return await self.apply_proposal(str(args.get("proposal_id") or ""))
        raise DispatchError(f"unknown tool: {name}")

    async def propose_ops(self, ops: list[dict], rationale: str) -> dict:
        doc = await self.db.get(Document, self.doc_id)
        if doc is None:
            raise DispatchError(f"document not found: {self.doc_id}")
        proposal_id = new_uuid()
        job_id = await self._ensure_job()
        ctx = await build_context(self.db, doc, self.data_dir)
        outcome = await apply_ops(ctx, ops, proposal_id, verify=self.verify)
        await self._handle_degraded(outcome, proposal_id, ops)

        verdict = "rejected"
        if outcome["applied_ops"] and not outcome["pending_ops"] and not outcome["dropped_ops"]:
            verdict = "accepted"
        elif outcome["applied_ops"] or outcome["pending_ops"]:
            verdict = "partial" if outcome["dropped_ops"] else "pending"
        if outcome["pending_ops"] and not outcome["applied_ops"]:
            verdict = "pending"

        proposal = CurationProposal(
            id=proposal_id,
            job_id=job_id,
            strategy="agent_chunk" if _touches_chunks(ops) else "agent_toc",
            run_id=self.run_id,
            rationale=rationale,
            payload_json=json.dumps({"ops": ops}, ensure_ascii=False),
            results_json=json.dumps(outcome["results"], ensure_ascii=False),
            verdict=verdict,
            applied_ops=outcome["applied_ops"],
            dropped_ops=outcome["dropped_ops"],
        )
        self.db.add(proposal)
        await self.db.flush()
        return {
            "proposal_id": proposal_id,
            "results": outcome["results"],
            "applied_ops": outcome["applied_ops"],
            "pending_ops": outcome["pending_ops"],
            "dropped_ops": outcome["dropped_ops"],
            "verify": outcome.get("verify"),
            "budget": self.budget.state(),
        }

    async def _handle_degraded(
        self, outcome: dict, proposal_id: str, ops: list[dict]
    ) -> None:
        """Ledger + lesson when verification rolled a write back.

        ``apply_ops`` already restored the pre-write snapshot; this leaves
        the machine record (auto_rollback outcome) and the negative lesson
        so the strategy ranking and the next run both learn from it.
        """
        verification = outcome.get("verify")
        if not verification or verification.get("verdict") != "worse":
            return
        from .verify import record_auto_rollback

        job = await self.db.get(CurationJob, self.job_id) if self.job_id else None
        await record_auto_rollback(
            self.db,
            doc_id=self.doc_id,
            issue_id=job.issue_id if job else "",
            kind=job.kind if job else "agent",
            strategy="agent_chunk" if _touches_chunks(ops) else "agent_toc",
            verify=verification,
            proposal_id=proposal_id,
            actor="curator",
        )

    async def _ensure_job(self) -> str:
        """A proposal needs a job row; a standalone call gets an agent job."""
        if self.job_id:
            return self.job_id
        from sqlalchemy import select

        fingerprint = "agent"
        row = (
            await self.db.execute(
                select(CurationJob).where(
                    CurationJob.issue_id == f"{self.doc_id}:agent",
                    CurationJob.fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            self.job_id = row.id
            return row.id
        job = CurationJob(
            id=new_uuid(),
            issue_id=f"{self.doc_id}:agent",
            doc_id=self.doc_id,
            kind="agent",
            fingerprint=fingerprint,
            status="running",
        )
        self.db.add(job)
        await self.db.flush()
        self.job_id = job.id
        return job.id

    async def get_proposal(self, proposal_id: str) -> dict:
        row = await self.db.get(CurationProposal, proposal_id)
        if row is None:
            raise DispatchError(f"proposal not found: {proposal_id}")
        return {
            "proposal_id": row.id,
            "verdict": row.verdict,
            "applied_ops": row.applied_ops,
            "dropped_ops": row.dropped_ops,
            "results": json.loads(row.results_json or "[]"),
            "budget": self.budget.state(),
        }

    async def apply_proposal(self, proposal_id: str) -> dict:
        """Approve the pending ops of an L2 proposal.

        Re-validates every pending op; anything that no longer passes is
        reported and skipped rather than applied blind.
        """
        row = await self.db.get(CurationProposal, proposal_id)
        if row is None:
            raise DispatchError(f"proposal not found: {proposal_id}")
        payload = json.loads(row.payload_json or "{}")
        results = json.loads(row.results_json or "[]")
        pending_indexes = [
            entry["index"] for entry in results if entry.get("status") == "pending"
        ]
        if not pending_indexes:
            return {"proposal_id": proposal_id, "applied": 0, "reason": "no pending ops"}
        ops = [payload.get("ops", [])[index] for index in pending_indexes]
        doc = await self.db.get(Document, self.doc_id)
        ctx = await build_context(self.db, doc, self.data_dir)
        outcome = await apply_ops(ctx, ops, proposal_id, verify=self.verify)
        await self._handle_degraded(outcome, proposal_id, ops)
        row.verdict = "accepted" if outcome["applied_ops"] else "rejected"
        row.applied_ops = (row.applied_ops or 0) + outcome["applied_ops"]
        merged = [entry for entry in results if entry.get("status") != "pending"]
        merged.extend(outcome["results"])
        row.results_json = json.dumps(merged, ensure_ascii=False)
        await self.db.flush()
        return {
            "proposal_id": proposal_id,
            "applied": outcome["applied_ops"],
            "results": outcome["results"],
            "verify": outcome.get("verify"),
            "budget": self.budget.state(),
        }


def _touches_chunks(ops: list[dict]) -> bool:
    return any(
        str(op.get("op") or "")
        in {"merge_chunks", "reassign_chunk", "drop_chunk", "split_chunk", "correct_text"}
        for op in ops
    )


def tool_schemas() -> list[dict]:
    """OpenAI-style tool definitions for ``llm_chat`` (P2) and MCP."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_pages_overview",
                "description": "One line per page: block count, label mix, chars, whether it is a TOC page, figure count.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "integer", "default": 0},
                        "limit": {"type": "integer", "default": 60},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_page_blocks",
                "description": "Typed OCR block stream for specific pages. label is verbatim; bbox is not a font-size proxy.",
                "parameters": {
                    "type": "object",
                    "properties": {"pages": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["pages"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_page_text",
                "description": "Plain text of specific pages (cheaper than blocks).",
                "parameters": {
                    "type": "object",
                    "properties": {"pages": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["pages"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_chunks",
                "description": "Slice list with index, node, pages, char count and an 80-char preview.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 100},
                        "offset": {"type": "integer", "default": 0},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_quality",
                "description": "Quality issues for this document (same enumeration as the quality page).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_usage",
                "description": "Usage log aggregates: queries that surfaced this document and whether hits were marked used.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_toc_nodes",
                "description": "Flat TOC node list with ids, parents, numbers, titles and page ranges.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_printed_toc_llm",
                "description": (
                    "An LLM reading of the printed TOC, as entries with printed "
                    "page, resolved page index and level. Use it to check the "
                    "stored TOC against the document's own contents page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "use_cache": {"type": "boolean", "default": True},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_ops",
                "description": "Submit a fix program. Each op is validated and returns accepted / pending_human / dropped with a reason.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ops": {"type": "array", "items": {"type": "object"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["ops"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_proposal",
                "description": "Read a proposal's status and per-op outcome.",
                "parameters": {
                    "type": "object",
                    "properties": {"proposal_id": {"type": "string"}},
                    "required": ["proposal_id"],
                },
            },
        },
    ]
