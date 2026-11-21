"""``python -m app.curator`` — run the curator, inspect proposals, report.

    python -m app.curator run --once [--limit 10] [--agent|--no-agent]
    python -m app.curator run --every 360          # periodic maintenance loop
    python -m app.curator report
    python -m app.curator replay [--days 7]      # zero-hit search queries, replayed
    python -m app.curator signals [--days 7]     # read-only patrol report
    python -m app.curator proposals [--doc ID] [--verdict pending]
    python -m app.curator audit                    # undo snapshots still present?
    python -m app.curator repair-snapshots         # backfill legacy snapshots
    python -m app.curator undo [--proposal ID]     # default: newest applied
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from sqlalchemy import select

from .config import get_settings
from .curation import runner
from .curation import lock as runner_lock
from .curation.outcomes import strategy_report
from .db import init_db, make_engine, session_factory
from .ingest.pipeline import set_engine
from .models import CurationProposal

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe(value: object, cap: int = 120) -> str:
    """Operator-facing text: strip terminal control sequences, bound length.

    Titles, queries, notes and error strings come from documents, users and
    external agents. Printing them raw lets ANSI escapes rewrite the
    operator's terminal.
    """
    return _CONTROL.sub(" ", str(value if value is not None else ""))[:cap]


def _print_run(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    mode = "agent" if result.get("agent") else "rules"
    print(
        f"run {result['run_id']}: {len(result['documents'])} document(s), "
        f"mode {mode}"
    )
    for doc in result["documents"]:
        applied = sum(job.get("applied", 0) for job in doc["jobs"])
        print(f"  {_safe(doc['title'], 40):42} applied {applied}")
        for job in doc["jobs"]:
            if job.get("auto_rolled_back"):
                state = "auto-rolled back (verification degraded)"
            else:
                state = job.get("skipped") or job.get("error") or (
                    f"applied {job.get('applied', 0)} / dropped {job.get('dropped', 0)}"
                )
            print(f"    {_safe(job.get('kind', '?'), 16):16} {_safe(state, 120)}")
    for job in result.get("search_jobs") or []:
        if job.get("pending"):
            state = "pending human approval"
        else:
            state = job.get("skipped") or job.get("error") or (
                f"applied {job.get('applied', 0)} / dropped {job.get('dropped', 0)}"
            )
        print(f"  [search] {_safe(job.get('query', ''), 40):42} {_safe(state, 120)}")
    print(f"total applied ops: {result['applied']}; skipped: {result['skipped']}")
    if result.get("timed_out"):
        print("达到墙钟预算，已提前结束；剩余文档留待下一轮。")


async def _run(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    use_agent = True if args.agent else (False if args.no_agent else None)
    if args.once:
        interval = 0.0
    elif args.every is not None:
        interval = max(0.0, float(args.every))
    else:
        # No flag: use the 编修 interval only when the user actually saved
        # settings (stored row). The shipped default is a UI suggestion, not
        # a reason to turn a one-shot `curator run` into a daemon.
        from sqlalchemy import select as _select

        from .models import AgentSetting

        async with session_factory(engine)() as db:
            row = (
                await db.execute(
                    _select(AgentSetting).where(AgentSetting.name == "bianxiu")
                )
            ).scalar_one_or_none()
        stored = (row.config if row else {}) or {}
        interval = max(0.0, float(stored.get("interval") or 0)) * 60
    try:
        while True:
            async with session_factory(engine)() as db:
                await runner.recover_stale_jobs(db)
                try:
                    result = await runner.run_once(
                        db,
                        settings.tocmemex_data_dir,
                        limit=args.limit,
                        max_chunk_chars=settings.tocmemex_max_chunk_chars,
                        use_agent=use_agent,
                        wall_clock_minutes=args.wall_clock,
                        verify=args.verify,
                    )
                except runner_lock.CuratorBusy as exc:
                    print(f"skipped: {exc}")
                    if not interval:
                        return 3
                    result = None
            if result is not None:
                _print_run(result, args.json)
            if not interval:
                return 0
            # Periodic maintenance: the loop is the scheduler. Each pass
            # takes the same single-instance lock, so a manual run and a
            # scheduled one never overlap.
            try:
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                raise
    except KeyboardInterrupt:
        print("stopped")
        return 0
    finally:
        await engine.dispose()


async def _undo(args) -> int:
    """Restore the pre-write state recorded for one proposal."""
    from sqlalchemy import select as _select

    from .curation.undo import undo_proposal

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            if not args.proposal:
                # The common human request is "undo the last thing the
                # curator did", which has no proposal id in hand.
                rows = (
                    (
                        await db.execute(
                            _select(CurationProposal)
                            .where(CurationProposal.applied_ops > 0)
                            .order_by(CurationProposal.created_at.desc())
                            .limit(1)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    print("no applied proposal to undo")
                    return 1
                args.proposal = rows[0].id
            try:
                result = await undo_proposal(
                    db, settings.tocmemex_data_dir, args.proposal
                )
            except (LookupError, ValueError) as exc:
                await db.rollback()
                print(str(exc))
                return 1
            await db.commit()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            mode = "精确快照" if result["stepwise"] else "回退到 adoption 基线"
            print(
                f"undid proposal {result['proposal_id'][:8]} "
                f"({mode}): {result['restored']['nodes']} nodes / "
                f"{result['restored']['chunks']} chunks"
            )
            if not result["stepwise"]:
                print(
                    "注意：该提案没有快照（早于快照功能），整份文档已回到"
                    "curator 介入前的基线。"
                )
        return 0
    finally:
        await engine.dispose()


async def _verify(args) -> int:
    """Measure one document now; show the verifier readings and recent outcomes."""
    from .curation.ops import build_context
    from .curation.verify import snapshot_metrics
    from .models import CurationOutcome, Document

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            doc = await db.get(Document, args.doc_id)
            if doc is None:
                print(f"document not found: {args.doc_id}")
                return 1
            ctx = await build_context(db, doc, settings.tocmemex_data_dir)
            metrics = await snapshot_metrics(ctx)
            outcomes = (
                (
                    await db.execute(
                        select(CurationOutcome)
                        .where(CurationOutcome.doc_id == doc.id)
                        .order_by(CurationOutcome.id.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
        payload = {
            "doc_id": doc.id,
            "title": doc.title,
            "metrics": metrics,
            "outcomes": [
                {
                    "action": row.action,
                    "verdict": row.verdict,
                    "survival": row.survival,
                    "kind": row.kind,
                    "strategy": row.strategy,
                    "created_at": row.created_at,
                    "verify": json.loads(row.verify_json) if row.verify_json else None,
                }
                for row in outcomes
            ],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"{_safe(doc.title, 60)}")
        for name, value in (metrics or {}).items():
            print(f"  {name:8} {value}")
        if outcomes:
            print("recent outcomes:")
            for row in outcomes:
                print(
                    f"  [{row.action:14}] {row.verdict:7} survival={row.survival:9} "
                    f"{row.kind or '-'} / {row.strategy or '-'} {row.created_at}"
                )
        return 0
    finally:
        await engine.dispose()


async def _repair_snapshots(args) -> int:
    """Copy adoption baselines into snapshots for legacy applied proposals."""
    from .curation.undo import backfill_snapshots

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            result = await backfill_snapshots(db, dry_run=args.dry_run)
            if not args.dry_run:
                await db.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        await engine.dispose()


async def _audit(args) -> int:
    """Check that every applied proposal still has an undoable pre-write state."""
    from .curation.snapshots import CurationSnapshot

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            applied = (
                (
                    await db.execute(
                        select(CurationProposal)
                        .where(CurationProposal.applied_ops > 0)
                        .order_by(CurationProposal.created_at)
                    )
                )
                .scalars()
                .all()
            )
            recorded = set(
                (
                    await db.execute(
                        select(CurationSnapshot.proposal_id).distinct()
                    )
                )
                .scalars()
                .all()
            )
        missing = [row for row in applied if row.id not in recorded]
        payload = {
            "applied_proposals": len(applied),
            "with_snapshot": len(applied) - len(missing),
            "missing_snapshot": len(missing),
            "missing": [
                {
                    "proposal_id": row.id,
                    "strategy": row.strategy,
                    "applied_ops": row.applied_ops,
                    "created_at": row.created_at,
                }
                for row in missing[:50]
            ],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"applied proposals: {payload['applied_proposals']}; "
                f"with snapshot: {payload['with_snapshot']}; "
                f"missing: {payload['missing_snapshot']}"
            )
            if missing:
                print(
                    "缺少快照的改动只能回退到 adoption 基线（整份文档），"
                    "不能逐次撤销："
                )
                for row in missing[:10]:
                    print(
                        f"  {row.id[:8]}  {row.strategy:14} "
                        f"applied={row.applied_ops}  {row.created_at}"
                    )
        return 0 if not missing else 1
    finally:
        await engine.dispose()


async def _report(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            rows = await strategy_report(db)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("no outcomes yet; run `python -m app.curator run --once`")
            return 0
        print(
            f"{'策略':<18}{'应用':>6}{'更好':>6}{'更差':>6}{'持平':>6}"
            f"{'存活':>6}{'回退':>6}{'观察中':>7}  平均 delta"
        )
        for row in rows:
            delta = ", ".join(f"{key} {value:+}" for key, value in row["delta"].items()) or "-"
            print(
                f"{row['strategy']:<18}{row['applied']:>6}{row['better']:>6}"
                f"{row['worse']:>6}{row['neutral']:>6}"
                f"{row.get('survived', 0):>6}{row.get('regressed', 0):>6}"
                f"{row.get('pending', 0):>7}  {delta}"
            )
        return 0
    finally:
        await engine.dispose()


async def _proposals(args) -> int:
    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            stmt = select(CurationProposal).order_by(CurationProposal.created_at.desc())
            if args.doc:
                from .models import CurationJob

                job_ids = (
                    (
                        await db.execute(
                            select(CurationJob.id).where(CurationJob.doc_id == args.doc)
                        )
                    )
                    .scalars()
                    .all()
                )
                stmt = stmt.where(CurationProposal.job_id.in_(job_ids))
            if args.verdict:
                stmt = stmt.where(CurationProposal.verdict == args.verdict)
            rows = (await db.execute(stmt.limit(args.limit))).scalars().all()
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "id": row.id,
                            "job_id": row.job_id,
                            "strategy": row.strategy,
                            "verdict": row.verdict,
                            "applied_ops": row.applied_ops,
                            "dropped_ops": row.dropped_ops,
                            "rationale": row.rationale,
                            "created_at": row.created_at,
                        }
                        for row in rows
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        for row in rows:
            print(
                f"{row.id[:8]}  {row.strategy:14} {row.verdict:9} "
                f"applied={row.applied_ops} dropped={row.dropped_ops}  {row.created_at}"
            )
        print(f"{len(rows)} proposal(s)")
        return 0
    finally:
        await engine.dispose()


async def _replay(args) -> int:
    """Replay recorded zero-hit queries against the current index."""
    from .curation.search_quality import replay_zero_hits

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            payload = await replay_zero_hits(
                db, days=args.days, limit=args.limit, top_k=args.top
            )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if not payload["replayed"]:
            print(
                f"近 {args.days} 天没有零命中查询。"
                "（MCP/Web 检索会把未命中记为 rank=0，CLI 检索不记账）"
            )
            return 0
        print(
            f"零命中查询回放（近 {payload['window_days']} 天，"
            f"{payload['replayed']} 条）：已恢复 {payload['recovered']}，"
            f"仍为 0 {payload['still_zero']}"
        )
        for item in payload["queries"]:
            state = f"{item['hits']} hits" if item["hits"] else "0 hits"
            docs = "、".join(_safe(doc["title"], 20) for doc in item["docs"][:3])
            suffix = f"  → {docs}" if docs else ""
            print(
                f"  {item['misses']:>3}× {item['tool']:<6} {state:<8} "
                f"{_safe(item['query'], 50)}{suffix}"
            )
        return 0
    finally:
        await engine.dispose()


async def _signals(args) -> int:
    """One read-only patrol report across quality, search and curation."""
    from .curation.signals import collect_signals

    settings = get_settings()
    engine = make_engine(settings.tocmemex_db_path)
    await init_db(engine)
    set_engine(engine)
    try:
        async with session_factory(engine)() as db:
            payload = await collect_signals(
                db,
                settings.tocmemex_data_dir,
                days=args.days,
                limit=args.limit,
            )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"巡逻信号（近 {payload['window_days']} 天）")
        issues = payload["quality_issues"]
        if issues:
            print("质量告警：")
            for item in issues:
                print(
                    f"  {item['kind']:<18} {item['groups']:>3} 组 / "
                    f"{item['count']:>4} 项 / {item['documents']:>3} 份文档"
                )
        else:
            print("质量告警：无")
        misses = payload["zero_hit_queries"]
        if misses:
            print("零命中查询：")
            for item in misses:
                print(
                    f"  {item['misses']:>3}× {_safe(item['tool'], 6):<6} "
                    f"{_safe(item['query'], 60)}"
                )
        unused = payload["unused_heat"]
        if unused:
            print("检索多但从未标记采用：")
            for item in unused:
                print(
                    f"  {item['searched']:>3} 次 / {item['queries']} 条查询  "
                    f"{_safe(item['title'], 40)}"
                )
        print(f"待批提案：{len(payload['pending_proposals'])}")
        review_tasks = payload.get("open_review_tasks") or []
        if review_tasks:
            print("待处理纲目工单：")
            for item in review_tasks:
                print(
                    f"  {_safe(item['status'], 8):<8} {_safe(item['doc_title'], 32):<32} "
                    f"{_safe(item['reason'], 60)}"
                )
        rollbacks = payload["auto_rollbacks"]
        if rollbacks:
            print("最近自动回滚：")
            for item in rollbacks:
                print(
                    f"  {_safe(item['kind'] or '-', 40)} / "
                    f"{_safe(item['strategy'] or '-', 40)}  "
                    f"{_safe(item['title'], 32)}  {_safe(item['note'], 160)}"
                )
        if payload["errata"]:
            print(
                "勘误："
                + "、".join(f"{status} {count}" for status, count in payload["errata"].items())
            )
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="curator")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one curator pass")
    run.add_argument("--once", action="store_true", help="run a single pass and exit")
    run.add_argument(
        "--every",
        type=float,
        default=None,
        metavar="MINUTES",
        help=(
            "keep running, sleeping this many minutes between passes; "
            "omit to use the 编修 interval from settings (hours → minutes)"
        ),
    )
    run.add_argument("--limit", type=int, default=runner.DEFAULT_DOC_LIMIT)
    agent_group = run.add_mutually_exclusive_group()
    agent_group.add_argument(
        "--agent",
        action="store_true",
        help="force the LLM agent loop (falls back to rules if no endpoint)",
    )
    agent_group.add_argument(
        "--no-agent",
        action="store_true",
        help="force the deterministic rules even when an LLM endpoint is configured",
    )
    run.add_argument(
        "--wall-clock",
        type=float,
        default=None,
        metavar="MINUTES",
        help=(
            "stop gracefully after this many minutes; omit to use the 编修 "
            f"settings (default {runner.DEFAULT_WALL_CLOCK_MINUTES})"
        ),
    )
    run.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "closed-loop verification after every write: measure before/after, "
            "roll back and write a negative lesson when metrics degrade "
            "(default on; --no-verify restores pre-verification behaviour)"
        ),
    )
    run.add_argument("--json", action="store_true")

    verify_cmd = sub.add_parser(
        "verify", help="measure one document now and show recent outcome survival"
    )
    verify_cmd.add_argument("doc_id")
    verify_cmd.add_argument("--json", action="store_true")

    report = sub.add_parser("report", help="strategy outcomes")
    report.add_argument("--json", action="store_true")

    replay = sub.add_parser(
        "replay", help="replay recorded zero-hit queries against the current index"
    )
    replay.add_argument("--days", type=int, default=7)
    replay.add_argument("--limit", type=int, default=50)
    replay.add_argument("--top", type=int, default=8)
    replay.add_argument("--json", action="store_true")

    signals = sub.add_parser(
        "signals", help="read-only patrol report: quality, search, curation signals"
    )
    signals.add_argument("--days", type=int, default=7)
    signals.add_argument("--limit", type=int, default=20)
    signals.add_argument("--json", action="store_true")

    proposals = sub.add_parser("proposals", help="list proposals")
    proposals.add_argument("--doc", default=None)
    proposals.add_argument("--verdict", default=None)
    proposals.add_argument("--limit", type=int, default=50)
    proposals.add_argument("--json", action="store_true")

    undo = sub.add_parser(
        "undo", help="undo one applied proposal (default: the newest)"
    )
    undo.add_argument("--proposal", default=None)
    undo.add_argument("--json", action="store_true")

    audit = sub.add_parser(
        "audit", help="check that applied proposals still have undo snapshots"
    )
    audit.add_argument("--json", action="store_true")

    repair = sub.add_parser(
        "repair-snapshots",
        help="give legacy applied proposals a snapshot from their adoption baseline",
    )
    repair.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    handlers = {
        "run": _run,
        "report": _report,
        "proposals": _proposals,
        "undo": _undo,
        "audit": _audit,
        "repair-snapshots": _repair_snapshots,
        "verify": _verify,
        "replay": _replay,
        "signals": _signals,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
