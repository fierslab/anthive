"""Chat-assistant agent loop.

Runs one turn of the conversation: takes the user's message + history,
streams the model's response via OpenAI-compatible chat completions
(OpenRouter by default), dispatches tool calls inline, and yields SSE
frames the FastAPI endpoint forwards to the web client.

Locked contract (per pika #217 §9):

    POST /api/chat
      body  : {"session_id": str, "message": str,
               "context": {"path": str}}
      stream: text/event-stream of `data: <json>\\n\\n` frames where
              json.type ∈ {"token", "action", "error", "done"}.

Persist-before-close: the full assistant turn (text + action frames)
must land in chat_history.db BEFORE the SSE stream emits ``done``.
The web client may navigate immediately on receiving an action frame;
without prior persistence, the post-nav rehydrate would lose the
just-completed turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from chat_config import ChatConfig
from chat_history import ChatHistory
from chat_prompt import (
    build_catalog_digest,
    build_system_prompt,
    build_user_envelope,
)
from chat_tools import (
    CLIENT_ACTION_TOOLS,
    TOOL_DEFS,
    ToolContext,
    dispatch_server_tool,
)


log = logging.getLogger('anthive.chat')


# Max tool-call iterations per turn. A reasonable cap so a misbehaving
# model doesn't loop indefinitely; in practice good models settle in 1-3.
_MAX_TOOL_ROUNDS = 8

# Per-LLM-call timeout. OpenRouter forwards to upstream providers
# (Anthropic, OpenAI, etc.) — wide enough for the longest tool-round
# answer, narrow enough that a stuck call doesn't hang ant-serve.
_LLM_TIMEOUT_SECONDS = 120.0

# Single retry-with-backoff on transient upstream errors (429 / 5xx).
# Per anthive-web #231: OpenRouter 429s on claude-opus-4 are usually
# momentary — one short backoff+retry hides most of them. Two retries
# would only help on a sustained burst; for that the user needs to add
# their own BYOK key to OpenRouter, which we cannot solve in code.
_RETRY_BACKOFF_SECONDS = 2.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _classify_upstream_error(exc: Exception) -> Tuple[Optional[int], Optional[int], str]:
    """Best-effort extraction of (status_code, retry_after_sec, friendly_message)
    from an OpenAI-SDK exception. Used both to decide whether to retry and to
    build the structured error frame web's client renders as a friendly card.
    """
    status_code: Optional[int] = None
    retry_after: Optional[int] = None

    # The OpenAI SDK exposes status_code on APIStatusError subclasses.
    sc = getattr(exc, 'status_code', None)
    if isinstance(sc, int):
        status_code = sc

    # Honour Retry-After if the provider set it.
    resp = getattr(exc, 'response', None)
    if resp is not None:
        try:
            ra = resp.headers.get('retry-after')
            if ra:
                # RFC 7231: integer seconds OR HTTP-date. We support seconds.
                retry_after = int(float(ra))
        except (AttributeError, ValueError, TypeError):
            pass

    # Best-guess fallback when no header — most OpenRouter 429s on opus
    # clear within a few seconds.
    if status_code == 429 and retry_after is None:
        retry_after = 5

    # Build a friendly message. The raw OpenRouter payload often includes
    # the depositor's user_id + provider chain — strip it for the user-
    # facing text but keep the upstream provider name when present.
    raw = str(exc)
    if status_code == 429:
        provider_match = re.search(r"'provider_name':\s*'([^']+)'", raw)
        provider = provider_match.group(1) if provider_match else None
        friendly = (
            "Model is temporarily rate-limited"
            + (f" by the upstream provider ({provider})" if provider else "")
            + ". Retry in a few seconds."
        )
    elif status_code and 500 <= status_code < 600:
        friendly = f"Upstream model service returned a transient error ({status_code}). Retry in a few seconds."
    else:
        friendly = raw

    return status_code, retry_after, friendly


def _sse(payload: Dict[str, Any]) -> bytes:
    """Encode one SSE frame: ``data: <json>\\n\\n``."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode('utf-8')


def _history_to_openai_messages(
    history: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Translate the stored transcript into OpenAI chat messages.

    Stored roles: 'user' | 'assistant' | 'action' | 'system'.
    'action' rows carry the SSE frame JSON in content — for replay
    purposes they're collapsed into the previous assistant message as
    a brief textual hint so the model has memory of what it did. The
    full action history is on the web side; we just need enough for
    coherent continuation.
    """
    out: List[Dict[str, Any]] = []
    for entry in history:
        role = entry.get('role')
        content = entry.get('content', '')
        if role in ('user', 'assistant', 'system'):
            out.append({'role': role, 'content': content})
        elif role == 'action':
            # Compact replay note attached to the last assistant message
            # (if any). Avoids the LLM seeing a raw tool_call shape it
            # didn't make this turn.
            try:
                frame = json.loads(content)
                desc = f"[performed client action {frame.get('name')}({frame.get('args', {})})]"
            except (ValueError, TypeError):
                desc = "[performed client action]"
            if out and out[-1]['role'] == 'assistant':
                out[-1]['content'] = (out[-1]['content'] + "\n" + desc).strip()
            else:
                # Orphan action — just drop. Shouldn't happen in practice.
                pass
    return out


async def _list_catalog_for_digest(
    base_url: str, auth: Optional[tuple], client: httpx.AsyncClient,
) -> List[Dict[str, Any]]:
    """Pull the current /databases list for the system-prompt digest."""
    try:
        resp = await client.get(
            f'{base_url.rstrip("/")}/databases',
            params={'per_page': 500},
            auth=auth, timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get('databases', [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("chat: catalog digest failed (%s) — system prompt will lack the digest", exc)
        return []


async def run_turn(
    *,
    cfg: ChatConfig,
    history_store: ChatHistory,
    user: str,
    session_id: str,
    message: str,
    context: Dict[str, Any],
    internal_api_url: str,
    internal_api_auth: Optional[tuple] = None,
) -> AsyncIterator[bytes]:
    """Run one assistant turn and yield SSE frames.

    Caller wraps the yield in a FastAPI ``StreamingResponse(..., media_type='text/event-stream')``.

    On any exception the function yields an ``error`` frame + ``done``
    and returns cleanly — the FastAPI handler never sees the exception.
    History is persisted before ``done`` is yielded.
    """
    # Local import — keeps openai out of the import path when chat is OFF.
    from openai import AsyncOpenAI  # type: ignore

    # 1. Persist the user message first so it lands in history even if
    #    the LLM call fails.
    history_store.append(user, session_id, 'user', message)

    # 2. Build the LLM messages array.
    async with httpx.AsyncClient(verify=False) as http_client:
        catalog = await _list_catalog_for_digest(
            internal_api_url, internal_api_auth, http_client,
        )
        system_prompt = build_system_prompt(catalog)
        prior_messages = _history_to_openai_messages(
            history_store.list_messages(user, session_id)
        )
        # The user's just-added turn is already in prior_messages as the
        # last 'user' entry. Replace it with the enveloped form (path-aware).
        for i in range(len(prior_messages) - 1, -1, -1):
            if prior_messages[i]['role'] == 'user':
                prior_messages[i]['content'] = build_user_envelope(message, context)
                break

        openai_messages: List[Dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            *prior_messages,
        ]

        # 3. Tool context for server-side dispatch.
        tool_ctx = ToolContext(
            base_url=internal_api_url,
            client=http_client,
            auth=internal_api_auth,
        )

        # 4. Set up the OpenAI client against OpenRouter (or whatever
        #    chat.base_url points at).
        ai = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=_LLM_TIMEOUT_SECONDS,
        )

        # 5. Tool-loop. Accumulates assistant text; emits SSE frames
        #    in real time; appends history + emits 'done' at the end.
        assistant_text_parts: List[str] = []
        action_frames_in_turn: List[Dict[str, Any]] = []

        async def _create_with_retry():
            """One retry-with-backoff on transient upstream errors. Per
            web #231: hides momentary OpenRouter 429s + 5xx without
            silently swapping models. If both attempts fail, re-raise
            the second exception so the outer except can build the
            structured error frame."""
            try:
                return await ai.chat.completions.create(
                    model=cfg.model,
                    messages=openai_messages,
                    tools=TOOL_DEFS,
                    stream=True,
                )
            except Exception as exc:  # noqa: BLE001
                status, _retry_after, _friendly = _classify_upstream_error(exc)
                if status in _RETRY_STATUSES:
                    log.info("chat: transient %s upstream — retrying once after %.1fs",
                             status, _RETRY_BACKOFF_SECONDS)
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    return await ai.chat.completions.create(
                        model=cfg.model,
                        messages=openai_messages,
                        tools=TOOL_DEFS,
                        stream=True,
                    )
                raise

        try:
            for round_idx in range(_MAX_TOOL_ROUNDS):
                stream = await _create_with_retry()

                # Accumulator for this round's tool-call shape.
                # OpenAI streams tool calls as fragments — collect by index.
                pending_tool_calls: Dict[int, Dict[str, Any]] = {}
                round_text_parts: List[str] = []
                finish_reason: Optional[str] = None

                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    # Plain text tokens.
                    if delta.content:
                        round_text_parts.append(delta.content)
                        yield _sse({'type': 'token', 'text': delta.content})

                    # Tool-call fragments — buffer until the stream ends.
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            slot = pending_tool_calls.setdefault(idx, {
                                'id': '',
                                'name': '',
                                'arguments': '',
                            })
                            if tc_delta.id:
                                slot['id'] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    slot['name'] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    slot['arguments'] += tc_delta.function.arguments

                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                # Round complete. Stitch into messages.
                text_this_round = ''.join(round_text_parts)
                if text_this_round:
                    assistant_text_parts.append(text_this_round)

                # Build the assistant message with any tool calls so the
                # next LLM round sees them.
                assistant_msg: Dict[str, Any] = {'role': 'assistant'}
                if text_this_round:
                    assistant_msg['content'] = text_this_round
                if pending_tool_calls:
                    assistant_msg['tool_calls'] = [
                        {
                            'id':       tc['id'],
                            'type':     'function',
                            'function': {
                                'name':      tc['name'],
                                'arguments': tc['arguments'] or '{}',
                            },
                        }
                        for tc in pending_tool_calls.values()
                    ]
                openai_messages.append(assistant_msg)

                if finish_reason == 'stop' or not pending_tool_calls:
                    break  # done — model has nothing more to do

                # Dispatch each tool call; append a 'tool' message per result.
                for tc in pending_tool_calls.values():
                    name = tc['name']
                    try:
                        args = json.loads(tc['arguments']) if tc['arguments'] else {}
                    except json.JSONDecodeError:
                        args = {}

                    if name in CLIENT_ACTION_TOOLS:
                        # Emit action frame to the client; tell the LLM it queued.
                        frame = {'type': 'action', 'name': name, 'args': args}
                        action_frames_in_turn.append(frame)
                        yield _sse(frame)
                        tool_result: Dict[str, Any] = {'queued': True}
                    else:
                        try:
                            tool_result = await dispatch_server_tool(name, args, tool_ctx)
                        except ValueError as exc:
                            tool_result = {'error': str(exc)}

                    openai_messages.append({
                        'role':        'tool',
                        'tool_call_id': tc['id'],
                        'content':     json.dumps(tool_result),
                    })

            else:
                # Hit _MAX_TOOL_ROUNDS without a 'stop' — model is looping.
                yield _sse({'type': 'error',
                            'message': f"chat: model exceeded {_MAX_TOOL_ROUNDS} tool rounds without stopping"})

        except Exception as exc:  # pylint: disable=broad-except
            log.warning("chat: agent error — %s", exc)
            status, retry_after, friendly = _classify_upstream_error(exc)
            # Structured error frame per web #231 — code + retry_after let
            # the client render an exact countdown / retry button instead of
            # parsing strings.
            err: Dict[str, Any] = {'type': 'error', 'message': friendly}
            if status is not None:
                err['code'] = status
            if retry_after is not None:
                err['retry_after'] = retry_after
            yield _sse(err)

        # 6. Persist the full assistant turn BEFORE emitting 'done'.
        #    Web client may navigate immediately on receiving an action,
        #    so the assistant text + each action frame must hit the DB
        #    before the SSE stream closes (locked per pika #217 §9.3).
        full_text = ''.join(assistant_text_parts).strip()
        if full_text:
            history_store.append(user, session_id, 'assistant', full_text)
        for frame in action_frames_in_turn:
            history_store.append_action(user, session_id, frame)

        # 7. Done frame — signals end of stream to the client.
        yield _sse({'type': 'done'})
