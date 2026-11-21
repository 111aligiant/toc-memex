"""The curator agent loop (ADR-0002 §9.3).

    messages = [system, user("处理文档 {doc_id}，目标见 quality")]
    for step in range(max_calls):
        reply = llm_chat(messages, tools=TOOLS)
        if not reply.tool_calls:
            break
        for call in reply.tool_calls:
            result = dispatcher.dispatch(call.name, call.args)

The system prompt carries invariants only — what may be changed, that every
claim needs evidence, and the budget. It deliberately says nothing about
which pages to read: that is the agent's judgment, and the whole point of
the tool surface (ADR-0002 §16.1 decision 2).
"""

from __future__ import annotations

import asyncio
import json

from ..curation.dispatch import Dispatcher, DispatchError, tool_schemas
from ..models import Document
from .client import ChatMessage, LLMError, llm_chat

SYSTEM_PROMPT = """\
你是 Toc Memex 档案库的维护 agent。一次只处理一份文档（{doc_id}：《{title}》）。

你只能改派生数据，不能改原文：
- 可以：合并/拆分切片、把切片改挂到别的目录节点、删除页眉页脚类的碎片切片、
  修摘要与关键词、在有证据时修正目录节点的页码、只做字符级的 OCR 混淆更正。
- 不可以：改原始文件、改 OCR 缓存、删除文档、跨文档写入。

硬性规则：
1. 任何改变页码、切片归属或拆分的操作都必须带 evidence，且 evidence 是原文片段，
   必须真的出现在你声称的页上。代码会逐条核对，核对失败的操作会被丢弃并返回原因。
2. 合并切片不得新增原文没有的字（代码按词元多重集核对，新增超过 5% 即拒绝）。
3. 高风险操作（改页码、合并/拆分目录节点）只会成为待人工批准的提案。
4. 预算是 {max_calls} 次工具调用。budget.calls_left 归零后 dispatch 会拒绝新调用，
   这时直接结束，已提交的提案会保留。
5. 证据充分就提案。被核对拒绝的操作不会写坏任何数据——只是返回原因让你重试或放弃，
   所以误提案的代价接近零，而漏掉一个真实问题没有任何机制能纠正它。
   只在证据确实矛盾（而非仅仅不完整）时才放弃，并说明放弃原因。
6. 结构性问题不要用 set_page 硬补。大范围页码错位、层级明显错误时，调用
   request_toc_rebuild（可指定争议子树根）把工单发给纲目复审；纲目重建的提案
   仍需人批，不会直接改库。

你看到的工具返回里有 label（OCR 类型）和 bbox。bbox 不是字号代理，不要用它判断层级。
先用 get_quality / get_toc_nodes / get_chunks 看现状，需要时再读页面。

结束时不要再调用工具，用一段文字总结：
- 处理了哪些告警、各提交了什么操作；
- 对每个看过但没提交的告警，一句话说明为什么（证据不足/证据矛盾/不在授权范围）。
这份总结会写给下一个处理该文档的 agent 和事后审计的人。
"""


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def render_system_prompt(template: str, *, doc_id: str, title: str, max_calls: int) -> str:
    """Fill the system prompt, falling back to the shipped text on bad braces.

    The prompt is user-editable from the 编修 settings page; a mistyped brace
    must not stop the maintenance run.
    """
    values = {"doc_id": doc_id, "title": title, "max_calls": max_calls}
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return SYSTEM_PROMPT.format(**values)


async def run_tool_loop(
    dispatcher: Dispatcher,
    doc: Document,
    *,
    max_calls: int = 20,
    run_id: str | None = None,
    settings=None,
    context: str = "",
    system_prompt: str | None = None,
) -> dict:
    """Run one document's agent loop; return steps, applied ops and errors.

    ``settings`` is the resolved endpoint (purpose / DB default / .env);
    without it the global .env settings were used, which is why a curator
    wired to a DB endpoint never reached this loop. ``context`` carries what
    previous agents learned about this document (hand-off notes and lessons)
    — the maintenance agent should start from that, not rediscover it.
    ``system_prompt`` overrides the shipped prompt (编修 settings page).
    """
    messages = [
        ChatMessage(
            role="system",
            content=render_system_prompt(
                system_prompt or SYSTEM_PROMPT,
                doc_id=doc.id,
                title=doc.title,
                max_calls=max_calls,
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                f"处理文档 {doc.id}。先用 get_quality 看这份文档的告警，"
                "再决定要不要读页面。"
                + (f"\n\n前人留下的上下文：\n{context}" if context else "")
            ),
        ),
    ]
    tools = tool_schemas()
    steps = 0
    applied = 0
    pending = 0
    proposals: list[str] = []
    error: str | None = None
    summary: str | None = None

    for step in range(max_calls):
        if dispatcher.budget.calls_left <= 0:
            break
        try:
            # llm_chat is synchronous and can block for seconds on the network;
            # calling it directly would freeze the whole event loop (every other
            # request stalls). Run it off-loop like agent/runner.py does.
            reply = await asyncio.to_thread(
                llm_chat,
                messages,
                settings,
                tools=tools,
                call_site="curator.tool_loop",
                run_id=run_id,
                doc_id=doc.id,
            )
        except LLMError as exc:
            error = str(exc)
            break
        steps += 1
        # The token budget is enforced here, not in dispatch: only the loop
        # sees each turn's usage. Over budget → stop after the tool results
        # of this turn land, so the assistant turn stays consistent.
        token_ok = dispatcher.budget.spend_tokens(
            (reply.tokens or {}).get("total") or 0
        )
        if not reply.tool_calls:
            # The no-tool turn is the agent's closing summary (see the system
            # prompt): kept on the result for the job record and audit.
            summary = (reply.content or "").strip() or None
            break

        # OpenAI-style APIs require the assistant message carrying tool_calls
        # to come BEFORE the tool results it refers to. Append it first, then
        # dispatch each call and append its tool result — never the reverse.
        assistant_tool_calls = []
        for call in reply.tool_calls:
            name = (call.get("function") or {}).get("name") or ""
            assistant_tool_calls.append(
                {
                    "id": call.get("id") or f"call_{step}_{name}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": (call.get("function") or {}).get("arguments") or "{}",
                    },
                }
            )
        messages.append(
            ChatMessage(
                role="assistant",
                content=reply.content,
                tool_calls=assistant_tool_calls,
            )
        )
        for call in reply.tool_calls:
            name = (call.get("function") or {}).get("name") or ""
            args = _parse_arguments((call.get("function") or {}).get("arguments"))
            try:
                result = await dispatcher.dispatch(name, args)
            except DispatchError as exc:
                result = {"error": str(exc)}
            except Exception as exc:  # a tool bug must not kill the loop
                result = {"error": f"{type(exc).__name__}: {exc}"}
            if name == "propose_ops" and isinstance(result, dict):
                applied += int(result.get("applied_ops") or 0)
                pending += int(result.get("pending_ops") or 0)
                if result.get("proposal_id"):
                    proposals.append(result["proposal_id"])
            messages.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=call.get("id") or f"call_{step}_{name}",
                    name=name,
                    content=json.dumps(result, ensure_ascii=False, default=str),
                )
            )
        if not token_ok:
            break

    return {
        "steps": steps,
        "applied": applied,
        "pending": pending,
        "proposals": proposals,
        "error": error,
        "summary": summary,
        "calls_left": dispatcher.budget.calls_left,
        "tokens_left": dispatcher.budget.tokens_left,
    }
