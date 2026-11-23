"""Backwards-compatible thin wrapper around ``scripts/run_eval.py retrieval``.

The real harness is ``scripts/run_eval.py``; this file keeps the old
``--queries`` invocation working for existing docs and habits::

    python scripts/eval_retrieval.py --db data/tocmemex.db --queries q.jsonl

It forwards to ``run_eval.py retrieval --golden <queries>`` and preserves
the historical exit code (1 when hit@k is below 0.9) unless the caller
passes an explicit ``--fail-under``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_eval import main as run_eval_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/tocmemex.db")
    parser.add_argument("--queries", required=True, help="golden JSONL (--golden)")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--fail-under",
        action="append",
        metavar="METRIC=VALUE",
        help="forwarded to run_eval.py; defaults to hit_at_k=0.9",
    )
    args, unknown = parser.parse_known_args(argv)

    forwarded = [
        "retrieval",
        "--db",
        args.db,
        "--golden",
        args.queries,
        "--top-k",
        str(args.top_k),
    ]
    if args.json:
        forwarded.append("--json")
    if args.out:
        forwarded += ["--out", args.out]
    thresholds = args.fail_under or ["hit_at_k=0.9"]
    for threshold in thresholds:
        forwarded += ["--fail-under", threshold]
    forwarded += unknown
    return run_eval_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
