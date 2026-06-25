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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


@dataclass(frozen=True)
class ChatConfig:
    """Resolved chat-assistant runtime configuration."""

    enabled: bool                              # config flag (chat.enabled in yaml)
    api_key: Optional[str]                     # from chat.api_key or OPENROUTER_API_KEY
    model: str                                 # default model id
    allowed_models: List[Dict[str, str]] = field(default_factory=list)
    # ↑ each entry: {id, label, provider, tier}. The picker on the web
    #   side reads this list; POST /api/chat refuses a model override
    #   that isn't in it.
    provider_order: Optional[List[str]] = None
    # ↑ OpenRouter provider-routing preference, e.g.
    #   ["Anthropic", "Amazon Bedrock"]. Passed via extra_body to the
    #   OpenAI-compatible /chat/completions call.
    base_url: str = 'https://openrouter.ai/api/v1'
    history_path: Path = Path('data/chat_history.db')

    @property
    def active(self) -> bool:
        """True iff the assistant should serve traffic.

        ``/api/chat/status`` returns ``{enabled: active}`` — i.e. NOT
        the raw ``chat.enabled`` flag, because the no-key silently-off
        state must look identical to disabled on the wire.
        """
        return self.enabled and bool(self.api_key)

    def allow_model(self, model_id: str) -> bool:
        """Whether ``model_id`` is in the picker. Empty allowed_models
        means 'no restriction' — only the default is in play."""
        if not self.allowed_models:
            return model_id == self.model
        return any(m.get('id') == model_id for m in self.allowed_models)


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

    # allowed_models: validate shape lightly — keep entries that have
    # at least an 'id', so a malformed user yaml degrades to default-only.
    raw_models = chat_block.get('allowed_models') or []
    allowed_models: List[Dict[str, str]] = []
    if isinstance(raw_models, list):
        for entry in raw_models:
            if isinstance(entry, dict) and entry.get('id'):
                allowed_models.append({
                    'id':       str(entry.get('id')).strip(),
                    'label':    str(entry.get('label') or entry.get('id')).strip(),
                    'provider': str(entry.get('provider') or '').strip(),
                    'tier':     str(entry.get('tier') or '').strip(),
                })

    provider_order = chat_block.get('provider_order')
    if isinstance(provider_order, list):
        provider_order = [str(p).strip() for p in provider_order if str(p).strip()]
        if not provider_order:
            provider_order = None
    else:
        provider_order = None

    return ChatConfig(
        enabled=enabled,
        api_key=api_key,
        model=model,
        allowed_models=allowed_models,
        provider_order=provider_order,
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

    Locked contract: ``{enabled: bool, model?: str, models?: [...]}``.
    Off state returns ``{enabled: false}`` — never leaks the configured
    default to a deployment that hasn't intentionally opted in.

    When active, ``models`` carries the picker entries
    (``[{id, label, provider, tier}, ...]``) so the web widget can
    render a model selector. Backward-compatible with the original
    ``{enabled, model}`` shape (clients that don't know about
    ``models`` ignore the field).
    """
    if not cfg.active:
        return {'enabled': False}
    payload: Dict[str, Any] = {
        'enabled': True,
        'model':   cfg.model,
    }
    if cfg.allowed_models:
        payload['models'] = cfg.allowed_models
    return payload
