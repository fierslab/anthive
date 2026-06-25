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

**Decision: Deep Chat web component.** Inline it in `base.html` behind a small
toggle button so it's on every page. If we later want richer action-rendering we
can revisit CopilotKit, but it's not worth a React build for v1.

## 5. Architecture

```
 browser (Deep Chat widget in base.html)
   │  POST /api/chat  {session_id, message}           (SSE stream back)
   ▼
 agent backend  ── Claude (tool-calling, streaming) ──┐
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

- **web session (this scope):** the Deep Chat widget in `base.html`, the
  client-side action tools (`navigate`/`set_plot_param`), the rehydrate-on-load
  glue, and the `set_plot_param` hook into `plot.html`'s existing
  `buildUrl`/form machinery.
- **anthive4 / ant-serve session:** the `POST /api/chat` agent endpoint
  (Claude tool-calling + streaming), the server-side data tools (thin wrappers
  over existing REST calls), the system-context assembly, and the session store.
  **This is where the Anthropic API key lives** and where data access already
  is — it does not belong in the web client.

The interface between them is the `POST /api/chat` SSE contract +
`GET /api/chat/history` + the client tool-call schema. Spec that, hand the
server half to anthive4.

## 7. Security & privacy

- **Behind Authelia** already; the assistant inherits the user's session and
  acts only through routes/endpoints that user can already reach. No privilege
  escalation surface beyond the existing API.
- **No raw SQL / no DuckDB-h5ad path** — tools are bounded REST wrappers, so
  there's no injection surface into the data layer.
- **Data egress**: questions + the catalogue digest + any tool results go to the
  Anthropic API. The data is scRNA-seq dataset *metadata* (not patient data), so
  this is very likely acceptable — but **confirm before wiring**, and document
  it. No `.h5ad` cell-level data is ever sent (the tools return summaries/URLs).
- **Rate / cost**: per-user rate limit on `/api/chat`; cap tool-call loops per
  turn; stream so a long turn is interruptible.

## 8. Phased plan (when/if scheduled)

1. **Spec the `/api/chat` contract** (SSE events, client tool-call schema,
   history endpoint). Hand the server half to anthive4.
2. **Server v1 (anthive4):** `/api/chat` with Claude + the five data tools +
   `build_plot_url`, system context from `/databases`, in-memory session store.
3. **Widget v1 (web):** Deep Chat in `base.html`, `navigate` action only,
   rehydrate from `/api/chat/history`. End-to-end "ask → land on the right view,
   conversation persists across the jump."
4. **`set_plot_param`** action wired into `plot.html` for in-place refinement
   ("make it a violin", "split by genotype") without leaving the page.
5. **Polish:** persistent session store (SQLite, TTL), htmx `hx-preserve` for
   flicker-free navigation, per-user rate limit, opt-out toggle.

## 9. Open questions

- **Anthropic API egress approval** for an internal KU Leuven tool — who signs
  off? (Blocks everything; resolve first.)
- **Session store lifetime** — per-browser-session only, or persistent history
  the user can revisit? Persistent is nicer but raises a "delete my chat data"
  obligation.
- **How aggressively should it act?** Auto-navigate on a confident answer, or
  propose a link the user clicks? Auto-navigate is the wow factor but can feel
  like loss of control — consider "navigate, with an undo/back affordance."
- **Scope of `set_plot_param`** — full parity with the plot form, or a curated
  subset of safe tweaks?
