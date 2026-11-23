"""Run + step tables for the agent console.

An ``AgentRun`` is one question; an ``AgentStep`` is one thing that happened
while answering it (an LLM turn, a tool call, a tool result, a note, the
answer). Steps are the timeline the console renders and are written as they
happen, so a browser polling the run sees progress without holding a
WebSocket.

``agent_steps`` is intentionally *not* a second source of truth for retrieval:
the run still writes ``usage_log`` (search / mark_used), ``activities``
(read_section) and ``agent_notes`` (add_note) through their normal paths.
The step row is the run-scoped view over those actions.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, new_uuid, utcnow_iso


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", index=True
    )
    # "console" = started by POST /api/runs (the built-in agent loop);
    # "mcp" = an external MCP connection, either via run_start or the first
    # observed tool call. The console renders both through the same timeline.
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="console", index=True
    )
    # MCP connection id and the client's self-declared name. Both identify a
    # session, not a model; session_id is server-assigned, client is not.
    session_id: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True
    )
    client: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The activity-layer task this run belongs to. Console runs answer a
    # question for the operator; an external MCP run is *work*, so it is
    # archived as a task and only its trace lives here. See external.py.
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    allow_notes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    # Ceiling on summed usage.total for this run; the loop stops (and fails)
    # once spending passes it. 0 = use the built-in default.
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prior_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Refreshed on every observed MCP call so a live session can be told
    # apart from one whose client vanished mid-run.
    last_seen_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    args_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(
        String(40), nullable=False, default=utcnow_iso
    )

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_step_run_seq"),
    )
