# DESIGN — in-app chat assistant (OPTIONAL / exploratory)

Status: **proposal, not scheduled.** Captures the architecture for an embedded,
app-aware LLM chat assistant in the anthive web UI. Nothing here is built.
Cross-ref: `web/CLAUDE.md` (the URL-driven view contract this leans on),
`bin/api/CLAUDE.md` + `doc/REST_API_SPEC.md` (the API the assistant's data tools
call), `doc/HANDOFF_authelia.md` (the identity it keys sessions on).

## 1. The idea

A chat box, present on every page, wired to an LLM that:

1. **Knows what the site can do** — the routes, the plot geoms, the live
   catalogue (datasets, genes, DE studies).
2. **Drives the view** — the user asks in words ("show me TREM2 across the
   microglia datasets", "now make that a violin split by genotype"), and the
   assistant navigates the browser to the right view / builds the right plot
   URL in the background.
3. **Keeps one session across page switches** — navigating to the view the
   assistant produced does **not** reset the conversation. You can ask for a
   plot, see it, ask for a refinement, and keep going in the same thread.

## 2. Why anthive is a good fit for this

**Every view and every plot is already a URL** (`/genes/{gene}`,
`/browse/{g}/{f}/plot/{geom}?x=…&y=…`). The hard problem in most "AI drives the
app" projects — mapping intent onto app actions — is largely *already solved
here*: an action is mostly "produce a URL and navigate to it." The assistant's
core job is **intent → URL**, with a handful of data-lookup tools to ground it.

This also bounds the blast radius: the assistant operates through the same
public contract (routes + REST API) the human UI uses. It cannot do anything a
user with a browser couldn't.

## 3. Goals / constraints

- **Embeds into the server-rendered Jinja site** — no SPA rewrite, no dragging
  React in for one widget if avoidable.
- **App-aware**: system context describes routes + geoms + live catalogue.
- **Acts through the existing contract**: client-side navigation + server-side
  tools that call the **existing REST API**. **Never raw SQL, never DuckDB/h5ad
  access** (the web UI's standing rule — see `web/CLAUDE.md` "What NOT to do").
- **Session survives full-page navigation** (our pages are full reloads).
- **Behind Authelia**; sessions keyed to the signed-in user.
- **Low custom UI code**; the effort should go into the agent + tools, not a
  bespoke chat widget.

## 4. Component landscape (the chat box itself)

The widget is the easy 20%. Surveyed options:

| Option | Fit | Notes |
|---|---|---|
| **Deep Chat** (`<deep-chat>`) | **chosen** | Framework-agnostic **web component**. Drops into a Jinja page with a `<script>` + one tag, no React. Streams, renders tool/function calls, custom request handler to our backend. The only mature embeddable that suits a vanilla-JS server-rendered site. |
| CopilotKit | closest in spirit | Purpose-built for "copilot reads app state + takes actions" (`useCopilotAction`, `useCopilotReadable`) — exactly this idea — but **React**. Would mean a React island in the page. |
| assistant-ui, Vercel AI SDK `useChat` | React only | Good UI shells; same React cost. |
| LibreChat / Open WebUI / Chatbot UI | rejected | Standalone apps, separate deploys — not embeddable. |
| Intercom / Tidio / Botpress | rejected | Support-bot SaaS; third-party data egress (unacceptable behind Authelia) and not built to drive our UI. |

**Decision (revised at implementation, 2026-06-25): a minimal inline vanilla
widget — *not* Deep Chat.** Deep Chat (and every option above) was rejected once
building started, because:

- This codebase is **strictly self-contained** (`web/CLAUDE.md`: no `static/`
  dir, inline CSS/JS, no external CDNs). An internal Authelia-gated box can't
  depend on `unpkg`, and bundling a ~hundreds-of-KB web component contradicts the
  inline convention.
- The thing Deep Chat would save us — the message UI — is ~150 lines of vanilla
  JS. The thing it *wouldn't* do — execute "agent emits action → browser
  navigates" — is custom either way.

So **v1 is a hand-written launcher + panel in `base.html`** (~CSS block + one
`<script>` IIFE), streaming via `fetch`+SSE, persisting session in
`sessionStorage` + server history. Built and verified 2026-06-25. If richer
action-rendering is ever wanted, revisit CopilotKit — but it has not been needed.

## 5. Architecture

```
 browser (Deep Chat widget in base.html)
   │  POST /api/chat  {session_id, message}           (SSE stream back)
   ▼
 agent backend  ── LLM via OpenRouter (OpenAI-compatible, tools, stream) ─┐
   │   executes SERVER tools ───────────────────┐     │
   │     list_datasets / get_dataset_info /      │     │
   │     gene_stats / list_de_studies /          ▼     │
   │     build_plot_url        ──────▶  existing REST API (ant-serve)
   │   emits CLIENT tool calls (navigate, set_param) ──┘
   ▼                                                   │
 session store (keyed by Authelia Remote-User)  ◀──────┘
   │
   └─ widget executes client tool calls: window.location = url / param tweak
```

### 5.0 LLM provider — OpenRouter (model-agnostic)

The agent talks to the model through **OpenRouter** (`https://openrouter.ai/api/v1`),
which is **OpenAI-compatible** — so the backend uses any OpenAI-style SDK/HTTP
client (Chat Completions, OpenAI-style `tools` + `tool_calls`, SSE streaming) and
the **model is a config string** (`chat.model`, e.g. `anthropic/claude-opus-4`,
`openai/gpt-...`, etc.). One key, swappable models, no provider lock-in. Caveats:

- **Tool-calling fidelity varies by model** — pick a strong tool-calling model
  (Claude- or GPT-4-class). Weak models mangle the action protocol.
- OpenRouter is an **extra intermediary** in the data path (see §8).
- The tool format is **OpenAI-style** (`tools`/`tool_calls`), not Anthropic's
  native blocks — relevant only to the server implementation.

### 5.1 Two tiers of tools

- **Client-side action tools** (executed in the browser by the widget):
  - `navigate(url)` — go to an in-app URL (a view or a plot page).
  - `set_plot_param(name, value)` — tweak a control on the current plot form
    and re-render (only when already on a plot page).
  - `highlight(selector)` — optional: draw attention to a UI element.
  The LLM emits these as tool calls; the widget's request handler runs them.

- **Server-side data tools** (executed by the agent backend, calling the REST
  API — **the same endpoints the UI already uses**):
  - `list_datasets(filters)` → `/databases` (+ the existing filter params).
  - `get_dataset_info(db_id)` → `/databases/{id}` + `/de`.
  - `gene_stats(gene, filters)` → `/gene_stats_all`.
  - `list_de_studies(db_id)` → `/databases/{id}/de`.
  - `build_plot_url(db_id, geom, params)` — pure URL construction + a validity
    check; returns the canonical interactive-page URL for `navigate`.
  No tool takes SQL. Each maps 1:1 to a bounded REST endpoint.

### 5.2 "Knows what the site can do" = system context

The system prompt is assembled per session from things we already compute:

- The **route map + geom list** (static; mirror the `web/CLAUDE.md` routes
  table + `_PLOT_GEOMS_UI`).
- A **compact live catalogue digest** (dataset ids, titles, organism/cell/
  disease, which have DE) — from `/databases`, cached.
- **Tool definitions** (§5.1).

Genes are *not* enumerated (too many) — the assistant uses `gene_stats` /
autocomplete to resolve a gene the user names.

### 5.3 Session that survives navigation

Our pages are full reloads, which destroy JS state — so the transcript must not
live only in the widget. Two layers:

1. **Server-side transcript store, keyed by Authelia `Remote-User`** (small
   SQLite or per-user JSON; TTL'd). On every page load the widget calls
   `GET /api/chat/history` and rehydrates. This is the correctness layer —
   survives reloads, new tabs, even a different device. ← do this first.
2. **(Later) htmx `hx-boost` + `hx-preserve`** on the chat container: turn
   in-app link clicks into AJAX content-swaps so the page never fully reloads
   and the widget stays mounted with zero rehydration flicker. Pure UX polish on
   top of layer 1; not required for correctness.

A `session_id` (per browser session, stored client-side) scopes one
conversation; the server store maps `Remote-User → {session_id → transcript}`.

## 6. Ownership boundary (who builds what)

- **web session (this scope):** the Deep Chat widget in `base.html` **gated on
  `GET /api/chat/status`** (no status → no widget), the client-side action tools
  (`navigate`/`set_plot_param`), the rehydrate-on-load glue, and the
  `set_plot_param` hook into `plot.html`'s existing `buildUrl`/form machinery.
- **anthive4 / ant-serve session:** the `POST /api/chat` agent endpoint
  (Claude tool-calling + streaming), the server-side data tools (thin wrappers
  over existing REST calls), the system-context assembly, the session store,
  **and the enablement gate** (§7): the `chat.enabled` flag, the key read from
  env/yaml, the `GET /api/chat/status` capability endpoint, and 404-when-off on
  the chat endpoints. **This is where the OpenRouter key lives** and where
  data access already is — it does not belong in the web client.

The interface between them is the `GET /api/chat/status` capability check, the
`POST /api/chat` SSE contract, `GET /api/chat/history`, and the client tool-call
schema. Spec that, hand the server half to anthive4.

## 7. Enablement & key safety (off by default, opt-in)

Hard requirements: the assistant is **off unless a deployment explicitly turns
it on**, and the OpenRouter API key is **optional and never committed**.

**Config — matches existing `anthive.yaml` practice.** `anthive.yaml` is already
**gitignored** and holds the S3 secrets; `anthive.default.yaml` (tracked) holds
safe defaults and `config.py` falls back to it. So:

- `anthive.default.yaml` ships `chat.enabled: false` — **off by default**; every
  fresh deployment starts dark and stays dark until someone opts in.
- A deployment opts in by setting `chat.enabled: true` in its **local, gitignored
  `anthive.yaml`**.
- The **API key comes from the environment** (`OPENROUTER_API_KEY`) — preferred,
  never in any file. As a fallback, `chat.api_key` may live in the gitignored
  `anthive.yaml` (consistent with how `s3.secret_access_key` already lives
  there). It **must never** appear in `anthive.default.yaml` or any tracked file.
- The **model is config**: `chat.model` (e.g. `anthropic/claude-opus-4`) in
  `anthive.default.yaml` with a sane default; `chat.base_url` defaults to
  OpenRouter but is overridable (any OpenAI-compatible endpoint). The model
  string is **not** a secret.

**Three-state availability (fail safe).** The server resolves one boolean at
startup:

| `chat.enabled` | key present (env or yaml) | result |
|---|---|---|
| false | — | **OFF** |
| true | no | **OFF** + one-line warning logged (never crash) |
| true | yes | **ON** |

"Enabled but keyless" degrades **silently to off** — the feature is optional, so
a missing key is never an error, never a 500, never a stub.

**Key hygiene.** Read once at startup; **never logged, never returned in any
response or error body, never sent to the client.** The browser learns only a
boolean.

**Two enforcement points (defense in depth):**

1. **Capability endpoint** `GET /api/chat/status` → `{"enabled": bool}` (no key,
   no secrets). `base.html` renders the chat widget **only when true** — a dark
   deployment shows no chat affordance at all.
2. **The endpoints themselves refuse when off.** `POST /api/chat` and
   `GET /api/chat/history` return **404** (not 403 — 404 hides that the feature
   exists) whenever availability is OFF. So a stale widget or a hand-crafted
   request hits nothing.

## 8. Security & privacy

- **Behind Authelia** already; the assistant inherits the user's session and
  acts only through routes/endpoints that user can already reach. No privilege
  escalation surface beyond the existing API.
- **No raw SQL / no DuckDB-h5ad path** — tools are bounded REST wrappers, so
  there's no injection surface into the data layer.
- **Data egress** — *approved (2026-06-25)*: questions + the catalogue digest +
  tool results go to **OpenRouter and the model provider it routes to**. The data
  is scRNA-seq dataset *metadata* (not patient data); no `.h5ad` cell-level data
  is ever sent (the tools return summaries/URLs). Egress is acceptable for this
  internal tool. Note OpenRouter is an **added intermediary** — set its
  account-level data policy to **disable prompt logging / opt out of training**,
  and avoid free models that mandate logging.
- **Rate / cost**: per-user rate limit on `/api/chat`; cap tool-call loops per
  turn; stream so a long turn is interruptible.

## 9. Wire contract (LOCKED — the web client is built to this, 2026-06-25)

The client half (`base.html`) is implemented and ships dark. The server half
(ant-serve) **must match these exactly** or the two won't meet. All three are
under `/api/` (proxied to ant-serve) and return **404 when chat is OFF** (§7).

**`GET /api/chat/status`** → `200 {"enabled": bool, "model"?: "<display string>"}`.
The only endpoint reachable when OFF — returns `{"enabled": false}` (never a
secret). The widget renders **only** when `enabled` is true.

**`GET /api/chat/history?session_id=<id>`** →
`200 {"messages": [{"role": "user"|"assistant"|"action", "content": "<text>"}]}`.
Authoritative transcript, keyed server-side by Authelia `Remote-User` ×
`session_id`. Empty list is fine (client falls back to its sessionStorage
mirror). `role:"action"` rows render as a muted "↪ …" note.

**`POST /api/chat`** body
`{"session_id": "<id>", "message": "<user text>", "context": {"path": "<url path+query>"}}`
→ **`text/event-stream`**. Each SSE frame is `data: <json>\n\n` where `<json>` is
one of:

| `type` | fields | client effect |
|---|---|---|
| `token` | `text` | append to the current assistant bubble (streaming) |
| `action` | `name`, `args` | execute a client action (below) |
| `error` | `message` | show a muted ⚠ note; turn ends |
| `done` | — | end of turn (stream close also ends it) |

**Client actions** (`type:"action"`):
- `navigate` — `args:{url}`: the client shows "↪ opening …", sets the re-open
  flag, and does `window.location.href = url` after ~400 ms. **The server MUST
  persist the full assistant turn (text + any action note) to the history store
  before the stream closes**, so the rehydrate on the next page is complete.
- `set_plot_param` — `args:{name, value, …}`: in-place plot tweak via a
  `window.__antApplyPlotParam` hook. **Hook not yet implemented** (phase 4);
  unknown/absent → silently ignored. For now the server should drive plot
  changes via `navigate` to a rebuilt plot URL.

**Config keys** (ant-serve): `chat.enabled` (default `false` in
`anthive.default.yaml`), `chat.model` (sane default), `chat.base_url` (default
OpenRouter), key from `OPENROUTER_API_KEY` env or `chat.api_key` (gitignored
yaml). See §7.

## 9a. Phased plan (when/if scheduled)

1. ~~Spec the contract~~ — **done** (§9, locked).
2. ~~Widget v1 (web)~~ — **done 2026-06-25**: inline launcher + panel in
   `base.html`, status-gated, SSE streaming, `navigate` action, session
   persisted across navigation, rehydrate from history. Ships dark.
3. **Server v1 (anthive4) — NEXT:** the enablement gate (§7) + `/api/chat/status`
   first, then `/api/chat` (OpenAI-compatible client → OpenRouter `chat.model`) +
   the five data tools + `build_plot_url`, system context from `/databases`,
   in-memory session store keyed by `Remote-User` × `session_id`.
4. **`set_plot_param`** — `window.__antApplyPlotParam` hook in `plot.html` for
   in-place refinement ("make it a violin", "split by genotype").
5. **Polish:** persistent session store (SQLite, TTL), htmx `hx-preserve` for
   flicker-free navigation, per-user rate limit, opt-out toggle.

## 10. Decisions & open questions

**Decided:**
- **LLM egress via OpenRouter** — approved (2026-06-25). Model is config
  (`chat.model`), provider-agnostic through OpenRouter's OpenAI-compatible API.
- **Off by default, explicit opt-in per deployment; key optional & never
  committed** — see §7 (config in gitignored `anthive.yaml`, capability endpoint
  + 404-when-off).

**Open:**
- **Session store lifetime** — per-browser-session only, or persistent history
  the user can revisit? Persistent is nicer but raises a "delete my chat data"
  obligation.
- **How aggressively should it act?** Auto-navigate on a confident answer, or
  propose a link the user clicks? Auto-navigate is the wow factor but can feel
  like loss of control — consider "navigate, with an undo/back affordance."
- **Scope of `set_plot_param`** — full parity with the plot form, or a curated
  subset of safe tweaks?
