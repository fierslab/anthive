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
import collections
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


# Max tool-call iterations per turn. A genuinely-stuck model needs an
# upper bound, but with SQL analytics in the toolset 8 was too tight:
# search_genes → analytics_schema → analytics_query (wrong column case)
# → analytics_query (fixed) → module_score → analytics_query is six
# productive rounds on its own. Bumped to 16; loop detector below
# catches the actual misbehaviour (same tool + same args repeated)
# earlier than the cap would.
_MAX_TOOL_ROUNDS = 16

# Loop detector. Abort early when the model calls THE SAME TOOL with
# THE SAME ARGS this many times back-to-back — that's stuck, not
# productive work.
_LOOP_DETECT_REPEAT = 3

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


async def _preflight_plot_url(
    nav_url: str,
    http_client: 'httpx.AsyncClient',
    internal_api_url: str,
    internal_api_auth: Optional[tuple],
) -> Dict[str, Any]:
    """Validate a `/browse/<db>/plot/<geom>?…` URL by GETting the underlying
    `/api/databases/<db>/plot/<geom>?…` form with tiny dimensions.

    Returns ``{'failed': bool, 'status': int|None, 'reason': str|None}``.
    A pass returns ``{'failed': False, …}``; the caller emits the navigate
    frame as usual. A fail surfaces the API's error body so the LLM can
    diff its previous params against the actual error.

    Latency: ~50-300 ms with width=2 height=2 — the params get validated
    in full but the matplotlib render is degenerate. Real navigation
    afterwards hits ant-serve's plot cache cleanly (different key, but
    the params are now known-valid).
    """
    import re
    from urllib.parse import urlsplit, parse_qs, urlencode

    # /browse/<group>/<file>/plot/<geom>?<params> →
    # <internal>/databases/<group>/<file>/plot/<geom>?<params>+tiny dims
    parts = urlsplit(nav_url)
    m = re.match(r'^/browse/([^/]+/[^/]+)/plot/([a-z0-9_]+)$', parts.path)
    if not m:
        # Not a plot-page URL — skip preflight.
        return {'failed': False, 'status': None, 'reason': 'not a plot URL'}

    db_id, geom = m.group(1), m.group(2)
    quoted_db = db_id.replace('/', '%2F')
    api_path = f'/databases/{quoted_db}/plot/{geom}'

    # Pile tiny dimensions on top of the user's params. The per-geom
    # endpoint enforces dpi ∈ [72, 600] (matplotlib raster minimum), so
    # the probe MUST use dpi ≥ 72 or preflight 422s on its own probe
    # rather than the user's params — that bug caused the "screen did
    # not change" + bot-hallucinated-success failure mode on
    # 2026-06-25/26. 2×2 inches at 72 dpi is still a 144×144 px render
    # → ~8 KB PNG, cost is rounding-error.
    q = parse_qs(parts.query, keep_blank_values=True)
    q['width'] = ['2']
    q['height'] = ['2']
    q['dpi'] = ['72']
    qs = urlencode(q, doseq=True)
    target = f'{internal_api_url.rstrip("/")}{api_path}?{qs}'

    try:
        resp = await http_client.get(
            target, auth=internal_api_auth, timeout=15.0,
        )
        if 200 <= resp.status_code < 300:
            return {'failed': False, 'status': resp.status_code, 'reason': None}
        # Try to extract the FastAPI {detail: ...} JSON error body.
        try:
            detail = resp.json().get('detail') or resp.text[:300]
        except (ValueError, AttributeError):
            detail = resp.text[:300]
        return {
            'failed': True,
            'status': resp.status_code,
            'reason': str(detail),
        }
    except httpx.HTTPError as exc:
        return {'failed': True, 'status': None, 'reason': f'preflight HTTP error: {exc}'}


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
    # Strip the legacy "[performed client action ...]" annotation that
    # the previous replay implementation appended directly to assistant
    # content — and that the model started MIMICKING when navigate was
    # preflight-blocked, claiming it had navigated when it hadn't. Old
    # sessions have these baked into stored assistant rows; scrubbing on
    # replay prevents the model from copying its own past hallucinations.
    legacy_action_re = re.compile(r"\s*\[performed client action[^\]]*\]\s*", re.IGNORECASE)

    out: List[Dict[str, Any]] = []
    for entry in history:
        role = entry.get('role')
        content = entry.get('content', '')
        if role == 'assistant' and isinstance(content, str):
            content = legacy_action_re.sub('', content).strip()
        if role in ('user', 'assistant', 'system'):
            out.append({'role': role, 'content': content})
        elif role == 'action':
            # Compact replay note attached to the last assistant message
            # so the model has memory of what client actions actually
            # fired. CRITICAL: the format MUST be unmistakably out-of-
            # voice — earlier versions used "[performed client action
            # navigate({'url': ...})]" and the model started mimicking
            # that string when a fresh navigate was BLOCKED by preflight,
            # pretending it had navigated when it hadn't. The HTML-ish
            # `<server-log …/>` tag is unlike anything the model writes
            # naturally, so it doesn't get copied. The prompt also tells
            # the model never to emit `<server-log` themselves.
            try:
                frame = json.loads(content)
                name = frame.get('name', '?')
                args = frame.get('args', {})
                desc = f"<server-log action={name} args={json.dumps(args, separators=(',', ':'))}/>"
            except (ValueError, TypeError):
                desc = "<server-log action=?/>"
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
    model_override: Optional[str] = None,
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
    #    the LLM call fails. Capture its seq so every usage row produced
    #    by this turn can carry the same `turn_seq` — lets us group
    #    rounds by turn for the cost-counter aggregation.
    turn_seq = history_store.append(user, session_id, 'user', message)

    # Resolve which model to use this turn — override (validated by
    # the endpoint) wins over the configured default. Needed up front
    # because the cache_control wrapping below conditions on it.
    active_model = model_override or cfg.model

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

        # System message — wrap as a content-block list with
        # cache_control: ephemeral when talking to an Anthropic model.
        # The system prompt + catalogue digest is the largest stable
        # payload across a session (~11k tokens today, dominated by the
        # ~150-dataset digest). Anthropic's prompt cache serves it at
        # ~10% of input price on subsequent calls within ~5 minutes,
        # which is the natural pace of an interactive chat. Non-Anthropic
        # models ignore the cache_control field; we keep the string form
        # for them so they don't see an unfamiliar shape.
        if active_model.startswith('anthropic/'):
            system_message: Dict[str, Any] = {
                'role': 'system',
                'content': [
                    {
                        'type': 'text',
                        'text': system_prompt,
                        'cache_control': {'type': 'ephemeral'},
                    }
                ],
            }
        else:
            system_message = {'role': 'system', 'content': system_prompt}

        openai_messages: List[Dict[str, Any]] = [
            system_message,
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

        # OpenRouter routing preference: pin provider order so we skip
        # the upstream provider tier that just 429'd us (Google Vertex AI
        # on Anthropic). Passed via OpenAI SDK's extra_body — OpenRouter
        # reads the `provider` field, every other compatible endpoint
        # ignores it. Only set when configured AND the chosen model is
        # from a multi-provider family (Anthropic). For non-Anthropic
        # models OpenRouter handles routing internally.
        extra_body: Dict[str, Any] = {}
        if cfg.provider_order and active_model.startswith('anthropic/'):
            extra_body['provider'] = {'order': cfg.provider_order}

        # 5. Tool-loop. Accumulates assistant text; emits SSE frames
        #    in real time; persists history + emits 'done' at the end.
        #
        # Persistence runs in a try/finally so the assistant turn lands
        # in history EVEN IF the client disconnects mid-stream (which it
        # does whenever an `action navigate` frame fires — the browser
        # does window.location, drops the SSE socket, cancels this
        # generator with CancelledError). Without the finally, the
        # post-navigate page rehydrate finds an empty assistant row
        # and the user thinks "the bot didn't reply".
        assistant_text_parts: List[str] = []
        action_frames_in_turn: List[Dict[str, Any]] = []
        _persisted = False

        def _persist_history_now() -> None:
            nonlocal _persisted
            if _persisted:
                return
            _persisted = True
            try:
                text = ''.join(assistant_text_parts).strip()
                if text:
                    history_store.append(user, session_id, 'assistant', text)
                for frame in action_frames_in_turn:
                    history_store.append_action(user, session_id, frame)
            except Exception as p_exc:  # noqa: BLE001
                log.warning("chat: history persist failed — %s", p_exc)

        async def _create_with_retry():
            """One retry-with-backoff on transient upstream errors. Per
            web #231: hides momentary OpenRouter 429s + 5xx without
            silently swapping models. If both attempts fail, re-raise
            the second exception so the outer except can build the
            structured error frame."""
            # `stream_options.include_usage=true` — OpenRouter sends a
            # final SSE chunk with a populated `.usage` (and per-token
            # cost) only when this is set. `extra_body.usage.include=true`
            # is the OpenRouter-side switch that surfaces cost +
            # prompt_tokens_details.cached_tokens for Anthropic prompt-
            # cache visibility.
            create_kwargs: Dict[str, Any] = dict(
                model=active_model,
                messages=openai_messages,
                tools=TOOL_DEFS,
                stream=True,
                stream_options={'include_usage': True},
            )
            usage_extra = dict(extra_body)
            usage_extra['usage'] = {'include': True}
            create_kwargs['extra_body'] = usage_extra
            try:
                return await ai.chat.completions.create(**create_kwargs)
            except Exception as exc:  # noqa: BLE001
                status, _retry_after, _friendly = _classify_upstream_error(exc)
                if status in _RETRY_STATUSES:
                    log.info("chat: transient %s upstream — retrying once after %.1fs",
                             status, _RETRY_BACKOFF_SECONDS)
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    return await ai.chat.completions.create(**create_kwargs)
                raise

        # Rolling window of tool-call signatures across this turn. If the
        # last _LOOP_DETECT_REPEAT entries are identical, the model is
        # stuck — abort with a specific message instead of letting it
        # ride to _MAX_TOOL_ROUNDS.
        recent_tool_sigs: collections.deque = collections.deque(maxlen=_LOOP_DETECT_REPEAT)
        loop_detected_tool: Optional[str] = None

        try:
            for round_idx in range(_MAX_TOOL_ROUNDS):
                stream = await _create_with_retry()

                # Accumulator for this round's tool-call shape.
                # OpenAI streams tool calls as fragments — collect by index.
                pending_tool_calls: Dict[int, Dict[str, Any]] = {}
                round_text_parts: List[str] = []
                finish_reason: Optional[str] = None
                # Captured from the FINAL chunk when stream_options.include_
                # usage=True. OpenRouter populates cost + cached-token
                # details here too. Kept as an opaque object; we read it
                # with getattr below.
                round_usage: Any = None

                async for chunk in stream:
                    # OpenRouter sends a usage-only chunk at end of
                    # stream when include_usage=True; it has no choices.
                    chunk_usage = getattr(chunk, 'usage', None)
                    if chunk_usage is not None:
                        round_usage = chunk_usage
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

                # Persist this LLM call's usage if OpenRouter surfaced
                # one. Pydantic/openai-typed object — read with getattr
                # so OpenRouter's extra fields (cost, cached_tokens)
                # don't trip a typed access. Best-effort; we never crash
                # the turn over a missing field.
                if round_usage is not None:
                    try:
                        prompt_tokens = int(getattr(round_usage, 'prompt_tokens', 0) or 0)
                        completion_tokens = int(getattr(round_usage, 'completion_tokens', 0) or 0)
                        cost_usd = float(getattr(round_usage, 'cost', 0.0) or 0.0)
                        prompt_details = getattr(round_usage, 'prompt_tokens_details', None)
                        cached_tokens = int(getattr(prompt_details, 'cached_tokens', 0) or 0) if prompt_details else 0
                        cache_write_tokens = int(getattr(prompt_details, 'cache_write_tokens', 0) or 0) if prompt_details else 0
                        history_store.append_usage(
                            user=user, session_id=session_id,
                            turn_seq=turn_seq, round_idx=round_idx,
                            model=active_model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cached_tokens=cached_tokens,
                            cache_write_tokens=cache_write_tokens,
                            cost_usd=cost_usd,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("chat: usage persist failed — %s", exc)

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

                    # Loop detector — track (tool name + args) sigs across
                    # the whole turn. If the last N are identical, the
                    # model is stuck; bail with a specific error.
                    try:
                        sig = f"{name}::{json.dumps(args, sort_keys=True, default=str)}"
                    except Exception:
                        sig = f"{name}::<unhashable args>"
                    recent_tool_sigs.append(sig)
                    if (len(recent_tool_sigs) >= _LOOP_DETECT_REPEAT
                            and len(set(recent_tool_sigs)) == 1):
                        loop_detected_tool = name
                        break

                    if name in CLIENT_ACTION_TOOLS:
                        # Server-side preflight for `navigate` to plot URLs:
                        # GET the underlying /api/databases/<db>/plot/<geom>
                        # form with tiny dimensions (validates params, near-
                        # zero render cost). If 4xx/5xx, DON'T emit the
                        # action; feed the error back to the LLM so it
                        # corrects without landing the user on a broken page.
                        # See _preflight_plot_url for the param translation.
                        preflight_info: Dict[str, Any] = {}
                        if name == 'navigate':
                            url = args.get('url') if isinstance(args, dict) else None
                            if isinstance(url, str) and '/plot/' in url:
                                # Yield a short status frame so the client
                                # can render a spinner during the silent
                                # preflight window. The 'info' type is
                                # additive per pika #235 — old clients can
                                # safely ignore it.
                                yield _sse({
                                    'type': 'info',
                                    'kind': 'preflight',
                                    'message': 'Validating plot params…',
                                })
                                preflight_info = await _preflight_plot_url(
                                    url, http_client,
                                    internal_api_url, internal_api_auth,
                                )

                        if preflight_info.get('failed'):
                            # Don't emit the action — feed the error back so
                            # the LLM can adjust + retry within the same
                            # turn (no need for the user to re-prompt).
                            tool_result: Dict[str, Any] = {
                                'navigate_blocked': True,
                                'reason': preflight_info.get('reason'),
                                'status': preflight_info.get('status'),
                                'url':    args.get('url') if isinstance(args, dict) else None,
                                'hint':   'The plot URL did not validate. '
                                          'Inspect the error, fix the params (column names, '
                                          'study/term/contrast, etc.) and try a corrected URL.',
                            }
                        else:
                            # Build the SSE frame per tool. `navigate` is
                            # an action frame; `ui_choice` / `ui_link` are
                            # rendering hints under type:'ui' so web can
                            # branch on the discriminator without
                            # action-frame collision.
                            if name == 'ui_choice':
                                frame = {
                                    'type':     'ui',
                                    'kind':     'choice',
                                    'question': args.get('question', '') if isinstance(args, dict) else '',
                                    'options':  args.get('options', []) if isinstance(args, dict) else [],
                                }
                            elif name == 'ui_link':
                                frame = {
                                    'type':        'ui',
                                    'kind':        'link',
                                    'url':         args.get('url', '') if isinstance(args, dict) else '',
                                    'label':       args.get('label', '') if isinstance(args, dict) else '',
                                    'description': (args.get('description', '') if isinstance(args, dict) else '') or None,
                                }
                            else:
                                frame = {'type': 'action', 'name': name, 'args': args}
                            action_frames_in_turn.append(frame)
                            yield _sse(frame)
                            tool_result = {'queued': True}
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

                if loop_detected_tool:
                    # Same tool + same args called _LOOP_DETECT_REPEAT
                    # times back-to-back — stuck. Emit a specific frame
                    # and stop the outer loop. Use 'info' (additive,
                    # web client treats it as soft notice) rather than
                    # 'error' so the partial work the model produced
                    # still renders without an alarming banner.
                    yield _sse({
                        'type':    'info',
                        'kind':    'loop_detected',
                        'message': (f"The assistant kept calling `{loop_detected_tool}` "
                                    f"with the same arguments {_LOOP_DETECT_REPEAT}× in a "
                                    f"row — stopping. Try a more specific question."),
                    })
                    break

            else:
                # Hit _MAX_TOOL_ROUNDS without a 'stop' — productive but
                # didn't converge. Soft notice, not an error: the partial
                # work already streamed to the user is real.
                yield _sse({
                    'type':    'info',
                    'kind':    'max_rounds',
                    'message': (f"The assistant made {_MAX_TOOL_ROUNDS} tool calls "
                                "and didn't reach a final answer. Send another "
                                "message to continue, or try a more focused question."),
                })

        except asyncio.CancelledError:
            # Client disconnected mid-stream — almost always happens
            # right after a `navigate` action: the browser does
            # window.location → SSE socket drops → asyncio cancels this
            # generator. Persist whatever we have so the post-nav page
            # rehydrate sees the just-completed turn, then re-raise so
            # the runtime cleans up normally.
            _persist_history_now()
            raise
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
        finally:
            # Belt-and-suspenders: every exit path (success, error,
            # cancellation) flushes the assistant turn to history. The
            # _persist_history_now closure is idempotent so it's safe to
            # call from both the happy-path block below AND here.
            _persist_history_now()

        # 6. Persist the full assistant turn BEFORE emitting 'done'.
        #    Idempotent — the finally above may have already run.
        _persist_history_now()

        # 7. Done frame — signals end of stream to the client.
        yield _sse({'type': 'done'})
