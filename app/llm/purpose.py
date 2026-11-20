"""Per-purpose agent settings: 纲目 (TOC parsing) and 编修 (curation agency).

Two things live here:

1. **Defaults** — the shipped prompt text and tuning values. Prompt defaults
   are imported lazily from ``app.ingest.llm_toc`` and ``app.llm.agent`` so the
   constants stay single-sourced; this module only names and merges them.

2. **Resolution** — ``resolve_runtime`` turns one agent's stored config into
   the ``Settings`` a call should use: purpose-bound endpoint first, then the
   DB default endpoint, then ``.env``. This mirrors
   ``app.llm.endpoints.active_endpoint``; a purpose field simply wins one step
   earlier, and nothing else about the fallback chain changes.

Stored config lives in ``agent_settings`` as one JSON row per agent
(``gangmu`` / ``bianxiu``) plus one ``global`` row for cross-agent knobs such
as the LLM call-log level. Rows hold only what the user changed; reads merge
over the defaults below, so upgrading the shipped prompts upgrades every
library that never edited them.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models import AgentSetting, ModelEndpoint

AGENTS = ("gangmu", "bianxiu")
GLOBAL = "global"

LOG_MODES = ("full", "meta", "off")


def _gangmu_prompts() -> dict[str, str]:
    from ..ingest import llm_toc

    return {
        "detect": llm_toc._TOC_DETECT_PROMPT,
        "transform": llm_toc._TOC_TRANSFORM_PROMPT,
        "verify": llm_toc._VERIFY_PROMPT,
        "fix": llm_toc._FIX_PROMPT,
    }


def _bianxiu_prompts() -> dict[str, str]:
    from .agent import SYSTEM_PROMPT

    return {"system_prompt": SYSTEM_PROMPT}


def gangmu_defaults() -> dict[str, Any]:
    from ..ingest.llm_toc import DEFAULT_CHECK_LIMIT, _FIX_MAX_ATTEMPTS, _VERIFY_SAMPLE_CAP

    return {
        "enabled": True,
        "endpoint": "",  # "" = follow the DB default endpoint
        "temperature": 0.0,
        "max_tokens": 4096,
        "retries": 1,
        "timeout": 120,
        "check_limit": DEFAULT_CHECK_LIMIT,
        "verify_sample": _VERIFY_SAMPLE_CAP,
        "fix_attempts": _FIX_MAX_ATTEMPTS,
        "prompts": _gangmu_prompts(),
    }


def bianxiu_defaults() -> dict[str, Any]:
    from ..curation import runner

    return {
        "endpoint": "",
        "temperature": 0.2,
        "max_tokens": 4096,
        "retries": 1,
        "timeout": 120,
        "interval": 6,  # hours; 0 = manual only
        "max_calls": 20,
        "wall_clock": runner.DEFAULT_WALL_CLOCK_MINUTES,
        "cooldown": runner.COOLDOWN_DAYS,
        "agent_kinds": list(runner.AGENT_KINDS),
        "prompts": _bianxiu_prompts(),
    }


def defaults_for(name: str) -> dict[str, Any]:
    if name == "gangmu":
        return gangmu_defaults()
    if name == "bianxiu":
        return bianxiu_defaults()
    raise ValueError(f"unknown agent: {name}")


def global_defaults() -> dict[str, Any]:
    return {"log_mode": "full"}


# ── validation ────────────────────────────────────────────────────────────


def _num(payload: dict, key: str, lo: float, hi: float, *, integer: bool) -> Any:
    value = payload.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是数字") from exc
    if number < lo or number > hi:
        raise ValueError(f"{key} 超出范围 {lo}–{hi}")
    return int(number) if integer else number


def validate(name: str, payload: dict) -> dict:
    """Whitelist and clamp one PUT body; unknown keys are dropped."""
    if name not in AGENTS:
        raise ValueError(f"unknown agent: {name}")
    base = defaults_for(name)
    out: dict[str, Any] = {}

    if name == "gangmu":
        if "enabled" in payload:
            out["enabled"] = bool(payload["enabled"])
    if "endpoint" in payload:
        endpoint = str(payload.get("endpoint") or "").strip()
        out["endpoint"] = endpoint
    for key, lo, hi, integer in (
        ("temperature", 0.0, 2.0, False),
        ("max_tokens", 256, 65536, True),
        ("retries", 0, 5, True),
        ("timeout", 10, 600, True),
    ):
        value = _num(payload, key, lo, hi, integer=integer)
        if value is not None:
            out[key] = value
    if name == "gangmu":
        for key, lo, hi in (
            ("check_limit", 1, 50),
            ("verify_sample", 1, 100),
            ("fix_attempts", 0, 5),
        ):
            value = _num(payload, key, lo, hi, integer=True)
            if value is not None:
                out[key] = value
    else:
        for key, lo, hi in (
            ("interval", 0, 720),
            ("max_calls", 1, 100),
            ("wall_clock", 1, 600),
            ("cooldown", 0, 90),
        ):
            value = _num(payload, key, lo, hi, integer=True)
            if value is not None:
                out[key] = value
        if "agent_kinds" in payload:
            from ..curation import runner

            kinds = payload.get("agent_kinds") or []
            if not isinstance(kinds, list):
                raise ValueError("agent_kinds 必须是列表")
            unknown = set(kinds) - set(runner.AGENT_KINDS)
            if unknown:
                raise ValueError(f"未知的问题类型：{', '.join(sorted(unknown))}")
            out["agent_kinds"] = [kind for kind in runner.AGENT_KINDS if kind in kinds]

    if "prompts" in payload:
        prompts = payload.get("prompts") or {}
        if not isinstance(prompts, dict):
            raise ValueError("prompts 必须是对象")
        allowed = set(base["prompts"])
        cleaned: dict[str, str] = {}
        for key, text in prompts.items():
            if key not in allowed:
                continue
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"提示词 {key} 不能为空")
            cleaned[key] = text
        out["prompts"] = {**base["prompts"], **cleaned}

    return out


# ── storage ───────────────────────────────────────────────────────────────


def _merge(base: dict, stored: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (stored or {}).items():
        if key == "prompts" and isinstance(value, dict):
            merged["prompts"] = {**merged.get("prompts", {}), **value}
        elif key in merged:
            merged[key] = value
    return merged


async def load_config(db: AsyncSession, name: str) -> dict:
    defaults = defaults_for(name)
    row = await db.get(AgentSetting, name)
    return _merge(defaults, row.config if row else {})


async def load_global(db: AsyncSession) -> dict:
    row = await db.get(AgentSetting, GLOBAL)
    return _merge(global_defaults(), row.config if row else {})


async def save_config(db: AsyncSession, name: str, payload: dict) -> dict:
    """Validate ``payload`` and persist it; return the merged effective config.

    Only changed values are stored, so an untouched field keeps tracking the
    shipped default after a code update.
    """
    from ..models import utcnow_iso

    cleaned = validate(name, payload)
    row = await db.get(AgentSetting, name)
    if row is None:
        row = AgentSetting(name=name, config=cleaned, updated_at=utcnow_iso())
        db.add(row)
    else:
        row.config = {**(row.config or {}), **cleaned}
        row.updated_at = utcnow_iso()
    await db.flush()
    return await load_config(db, name)


async def save_log_mode(db: AsyncSession, mode: str) -> str:
    from ..models import utcnow_iso

    if mode not in LOG_MODES:
        raise ValueError("log mode 必须是 full / meta / off")
    row = await db.get(AgentSetting, GLOBAL)
    if row is None:
        db.add(AgentSetting(name=GLOBAL, config={"log_mode": mode}, updated_at=utcnow_iso()))
    else:
        row.config = {**(row.config or {}), "log_mode": mode}
        row.updated_at = utcnow_iso()
    await db.flush()
    return mode


# ── resolution ────────────────────────────────────────────────────────────


async def _purpose_endpoint(
    db: AsyncSession, endpoint_id: str, base: Settings
) -> tuple[Settings, str] | None:
    """Settings for one bound endpoint; None means "fall back a step"."""
    if not endpoint_id:
        return None
    row = await db.get(ModelEndpoint, endpoint_id)
    if row is None or not row.enabled:
        return None
    if not (row.api_key and row.base_url and row.model):
        return None
    return (
        base.model_copy(
            update={
                "tocmemex_llm_api_key": row.api_key,
                "tocmemex_llm_base_url": row.base_url,
                "tocmemex_llm_model": row.model,
                "tocmemex_llm_reasoning_effort": row.reasoning_effort
                or base.tocmemex_llm_reasoning_effort,
            }
        ),
        row.name,
    )


async def resolve_runtime(
    db: AsyncSession, name: str, base: Settings | None = None
) -> tuple[Settings | None, str | None, dict]:
    """Resolve the settings one agent call should use.

    Order: the agent's bound endpoint → the DB default endpoint → ``.env``.
    Returns ``(settings, endpoint_name, config)``; ``settings`` is None when
    no endpoint is usable at all, in which case callers take the
    deterministic path.
    """
    from .endpoints import active_endpoint

    base = base or Settings()
    cfg = await load_config(db, name)
    global_cfg = await load_global(db)

    picked = await _purpose_endpoint(db, cfg.get("endpoint") or "", base)
    endpoint_name: str | None = picked[1] if picked else None
    settings: Settings | None = picked[0] if picked else None

    if settings is None:
        resolved = await active_endpoint(db, base)
        if resolved is not None and (resolved["api_key"] and resolved["base_url"] and resolved["model"]):
            settings = base.model_copy(
                update={
                    "tocmemex_llm_api_key": resolved["api_key"],
                    "tocmemex_llm_base_url": resolved["base_url"],
                    "tocmemex_llm_model": resolved["model"],
                    "tocmemex_llm_reasoning_effort": resolved["reasoning_effort"]
                    or base.tocmemex_llm_reasoning_effort,
                }
            )
            endpoint_name = resolved["name"]

    if settings is None:
        return None, None, cfg

    settings = settings.model_copy(
        update={
            "tocmemex_llm_temperature": float(cfg.get("temperature", 0.0)),
            "tocmemex_llm_max_tokens": int(cfg.get("max_tokens", 4096)),
            "tocmemex_llm_retries": int(cfg.get("retries", 1)),
            "tocmemex_llm_timeout": float(cfg.get("timeout", 120)),
            "tocmemex_llm_log": global_cfg.get("log_mode")
            or settings.tocmemex_llm_log,
        }
    )
    if name == "gangmu":
        settings = settings.model_copy(update={"tocmemex_gangmu": cfg})
    return settings, endpoint_name, cfg