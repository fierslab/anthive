"""Chat-assistant configuration + three-state gate.

The chat assistant has THREE possible runtime states, all derived from
``anthive.yaml``'s ``chat:`` block plus the ``OPENROUTER_API_KEY`` env
override:

  - **ACTIVE**   — ``chat.enabled: true`` AND an API key is present.
                   ant-serve exposes the full ``/api/chat/*`` surface,
                   the web client renders the widget.
  - **SILENTLY OFF** — ``chat.enabled: true`` but NO API key (neither
                   ``chat.api_key`` nor ``OPENROUTER_API_KEY``). The
                   server logs a one-line warning at startup, and
                   ``/api/chat/status`` returns ``{enabled: false}`` so
                   the widget stays hidden. Locked contract per web's
                   §7 — never crash on a missing key.
  - **DISABLED** — ``chat.enabled: false`` (default). ``status``
                   returns ``{enabled: false}``; every other chat
                   endpoint returns 404.

The web client only invokes ``/api/chat/status`` to discover state, so
the wire surface is the same in the SILENTLY-OFF and DISABLED cases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class ChatConfig:
    """Resolved chat-assistant runtime configuration."""

    enabled: bool                    # config flag (chat.enabled in yaml)
    api_key: Optional[str]           # from chat.api_key or OPENROUTER_API_KEY
    model: str                       # OpenAI-compatible model identifier
    base_url: str                    # OpenAI-compatible API endpoint
    history_path: Path               # SQLite DB for per-user chat history

    @property
    def active(self) -> bool:
        """True iff the assistant should serve traffic.

        ``/api/chat/status`` returns ``{enabled: active}`` — i.e. NOT
        the raw ``chat.enabled`` flag, because the no-key silently-off
        state must look identical to disabled on the wire.
        """
        return self.enabled and bool(self.api_key)


def _coerce_bool(value: Any) -> bool:
    """Permissive yaml-bool reader; lots of users write 'true' as a string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(value)


def load_chat_config(
    raw_config: Dict[str, Any],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> ChatConfig:
    """Resolve the chat config block + env override into a ChatConfig.

    Args:
        raw_config: the merged anthive.yaml dict (typically from
            ``config.get_config()``).
        env: override for ``os.environ`` — useful in tests.

    The yaml ``chat.api_key`` is the canonical source per Mark's
    decision (pika #226); ``OPENROUTER_API_KEY`` env var is an optional
    override so a deployment can swap the key without editing the
    gitignored yaml.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    chat_block: Dict[str, Any] = raw_config.get('chat') or {}

    enabled = _coerce_bool(chat_block.get('enabled', False))

    # env wins if set; otherwise yaml; otherwise None
    api_key = env_map.get('OPENROUTER_API_KEY') or chat_block.get('api_key') or None
    if isinstance(api_key, str):
        api_key = api_key.strip() or None

    model = (chat_block.get('model') or 'anthropic/claude-opus-4').strip()
    base_url = (chat_block.get('base_url')
                or 'https://openrouter.ai/api/v1').strip()
    history_path = Path(chat_block.get('history_path') or 'data/chat_history.db')

    return ChatConfig(
        enabled=enabled,
        api_key=api_key,
        model=model,
        base_url=base_url,
        history_path=history_path,
    )


def warn_if_misconfigured(cfg: ChatConfig, logger) -> None:
    """Emit a single startup warning when chat is enabled but unkeyed.

    Spec from web §7: 'enabled but no key → silently OFF + one-line
    warning (never crash)'. Called once from ant-serve's startup;
    safe no-op when chat is disabled or active.
    """
    if cfg.enabled and not cfg.api_key:
        logger.warning(
            "chat: chat.enabled=true but no api_key in anthive.yaml AND no "
            "OPENROUTER_API_KEY in the env — chat assistant is OFF (widget "
            "stays hidden). Set chat.api_key in the gitignored anthive.yaml "
            "or export OPENROUTER_API_KEY to activate."
        )


def status_payload(cfg: ChatConfig) -> Dict[str, Any]:
    """Build the ``GET /api/chat/status`` JSON body.

    Locked contract: ``{enabled: bool, model?: str}``. ``model`` is
    only returned when ``enabled`` is true so the web client can render
    the model name in the widget — never returned in the off path
    (could leak the configured default to a deployment that hasn't
    intentionally opted in).
    """
    if not cfg.active:
        return {'enabled': False}
    return {'enabled': True, 'model': cfg.model}
