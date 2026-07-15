# Guardrailed LLM Gateway

## Overview

A small, production-minded FastAPI service that sits between clients and an LLM
provider. It streams model responses over Server-Sent Events, enforces a
concurrency-safe per-user daily request budget in Redis, fails open when Redis
is unavailable, bounds LLM latency with deliberate timeouts, and turns every
failure into a safe user experience with structured telemetry. The design goal
is the *smallest robust system* that demonstrates production judgment — not the
most features.

```
Client → FastAPI Gateway → Budget Enforcement (Redis) → LLM Gateway (LiteLLM/OpenRouter) → SSE stream
```

## Live Deployment

- Live URL: `TODO: <render-url>` (see [Deployment](#deployment))
- Loom demo: `TODO: <loom-url>`

## Architecture

```
                         ┌───────────────────────────┐
  Client  ──POST /chat──▶│ FastAPI app (main.py)      │
                         │  • request_id middleware   │
                         │  • validation handler      │
                         └────────────┬──────────────┘
                                      │
                         ┌────────────▼──────────────┐
                         │ Chat route (api/routes.py) │
                         │  validate → budget → stream│
                         └──────┬──────────────┬──────┘
                                │              │
                   ┌────────────▼───┐   ┌──────▼───────────────┐
                   │ BudgetService  │   │ ChatService          │
                   │ (services/     │   │ (services/chat.py)   │
                   │  budget.py)    │   │  SSE + metrics +     │
                   │  atomic INCR   │   │  fallback semantics  │
                   └───────┬────────┘   └──────┬───────────────┘
                           │                   │
                     ┌─────▼─────┐      ┌───────▼────────────┐
                     │  Redis    │      │ LLMGateway         │
                     │           │      │ (services/llm.py)  │
                     └───────────┘      │  LiteLLM→OpenRouter│
                                        └────────────────────┘
```

Modules and single responsibilities:

| Module | Responsibility |
|---|---|
| `app/main.py` | App factory, lifespan (Redis + service wiring), request-ID middleware, validation error handler |
| `app/api/routes.py` | Thin handlers: validate → budget decision → 429 or stream; `/health`, `/ready` |
| `app/api/deps.py` | Dependency accessors for injected services (testable) |
| `app/core/config.py` | Env-driven `Settings` with fail-fast validation |
| `app/core/logging.py` | Stdlib JSON formatter for structured logs |
| `app/schemas/chat.py` | Request validation (non-empty, length limits, whitespace) |
| `app/services/budget.py` | Atomic, concurrency-safe daily budget; fail-open |
| `app/services/llm.py` | Only module that knows LiteLLM; streaming + timeouts + error normalization |
| `app/services/chat.py` | Orchestrates the stream, SSE serialization, fallback, final request log |

## Request Flow

1. Client sends `POST /chat`.
2. Middleware assigns a `request_id` (sanitized `X-Request-ID` header if valid, else a new UUID).
3. Request timer starts; body is validated (`user_id`, `message` non-empty and within limits).
4. `BudgetService.consume()` atomically increments the user's daily counter in Redis.
   - **Over budget** → structured log with `throttled=true`, HTTP `429` returned **before** any LLM call.
   - **Redis unavailable** → degradation logged, request **allowed** (`budget_enforcement=degraded`).
5. `ChatService.stream_sse()` opens the LLM stream and forwards tokens immediately as SSE `token` events.
6. Time-to-first-token, provider latency, and generated character count are tracked.
7. On LLM failure/timeout → a safe SSE `error` event is emitted, then `done`; the stream terminates.
8. Stream always ends with a `done` event carrying the `request_id`.
9. A final structured log records total latency, estimated tokens, throttled status, budget enforcement mode, and LLM outcome.

## API

### `POST /chat`

Request:

```json
{ "user_id": "user-123", "message": "Help me prepare for a backend interview." }
```

Streaming success response (`text/event-stream`):

```
event: token
data: {"content": "Hello"}

event: token
data: {"content": " there"}

event: done
data: {"request_id": "..."}
```

Streaming fallback (failure after headers are sent):

```
event: error
data: {"message": "I'm having trouble responding right now. Please try again shortly."}

event: done
data: {"request_id": "..."}
```

Over-budget response — HTTP `429` (before streaming begins), with `Retry-After`:

```json
{ "error": { "code": "daily_budget_exceeded", "message": "Daily request limit exceeded. Please try again tomorrow." } }
```

Invalid input — HTTP `422`:

```json
{ "error": { "code": "invalid_request", "message": "Request validation failed. Check user_id and message." } }
```

### `GET /health`

Liveness. No dependency checks. Always fast. `{"status": "ok"}`.

### `GET /ready`

Readiness. Pings Redis with a short timeout. `200` when reachable, `503` when not.

```json
{ "status": "ready", "dependencies": { "redis": "ok" } }
```

## Local Setup

### Prerequisites

- **Python 3.12** (the deploy runtime; newer versions can't build `pydantic-core` wheels).
- **Docker** (to run Redis locally).
- An **OpenRouter API key** — sign up at [openrouter.ai](https://openrouter.ai) → create a key (`sk-or-...`). This is the only external secret needed.

### Step-by-step

```bash
# 1. Create the virtualenv and install dependencies
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Start Redis (mapped to host port 6380 to avoid clashing with any Redis on 6379)
docker compose up -d redis

# 3. Create your .env and add your key
cp .env.example .env
#    → open .env and set:  LLM_API_KEY=sk-or-...

# 4. Load the env vars and run the server
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

The server is now on **http://localhost:8000**.

### Verify it works

```bash
# liveness
curl http://localhost:8000/health
# → {"status":"ok"}

# readiness (checks Redis)
curl http://localhost:8000/ready
# → {"status":"ready","dependencies":{"redis":"ok"}}

# streaming chat (watch tokens arrive live)
curl -N -X POST http://localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"user_id":"me","message":"Say hi in 5 words."}'
# → event: token / data: {"content":"..."}  ... ending with  event: done
```

### Alternative: everything in Docker

```bash
LLM_API_KEY=sk-or-... docker compose up --build
```

This runs Redis **and** the gateway together (gateway reaches Redis over the
internal Docker network, so the 6380 host mapping doesn't matter here).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `local` | `production` enables strict config validation (requires `LLM_API_KEY`) |
| `LOG_LEVEL` | `INFO` | Log level |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection. `.env.example` uses `:6380` to match `docker compose`; Railway injects this in prod |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | `0.5` | Short connect timeout so `/chat` fails open fast |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `0.5` | Short command timeout |
| `DAILY_REQUEST_LIMIT` | `20` | Requests per user per UTC day |
| `LLM_MODEL` | `openrouter/openai/gpt-4o-mini` | LiteLLM model string |
| `LLM_API_KEY` | _(empty)_ | Provider key; never committed |
| `LLM_TIMEOUT_SECONDS` | `30` | Total budget for the whole LLM operation |
| `LLM_CHUNK_TIMEOUT_SECONDS` | `15` | Max idle time between streamed chunks (0 disables) |
| `MAX_USER_ID_LENGTH` | `128` | Input guard |
| `MAX_MESSAGE_LENGTH` | `8000` | Input guard |

## Testing

Tests use `fakeredis` (in-memory) and a `FakeLLMGateway` — **no real Redis and no
paid API calls**, so they run in well under a second and cost nothing.

```bash
# run the whole suite (from the repo root, venv activated)
pytest

# more detail / a single file / a single test
pytest -v
pytest tests/test_budget.py
pytest tests/test_chat.py -k timeout
```

Expected: **17 passed**. You do **not** need Redis running or an API key to run
the tests — they mock both dependencies.

### What each test file covers

| File | Critical behavior verified |
|---|---|
| `tests/test_budget.py` | Under/over limit, TTL is set, **20 concurrent requests never exceed the limit**, Redis failure → fail-open, `ping` readiness |
| `tests/test_chat.py` | Incremental streaming, **429 returned before the LLM is called**, Redis-down still invokes LLM, LLM error/timeout → safe fallback (no stack trace), `done` terminates the stream, invalid input → 422 |
| `tests/test_health.py` | `/health` returns 200 with no dependency check; `/ready` returns 200 when Redis is up, 503 when down |

## Failure Behavior

| Failure | User sees | Server does |
|---|---|---|
| **Redis outage** | `/chat` still works normally | Warning log `budget_check_degraded` with error class; request allowed; `/ready` returns `503` |
| **LLM timeout** | Safe `error` + `done` SSE events | Bounded by total + per-chunk timeouts; `llm_outcome=error:timeout` logged |
| **LLM provider failure** | Safe `error` + `done` SSE events | No stack trace/provider internals; `llm_outcome=error:<class>` logged |
| **Budget exhausted** | HTTP `429` with `Retry-After` | LLM never called; `throttled=true` logged |
| **Failure after headers sent** | `error` event, then `done`, stream ends | Status stays `200` (cannot change post-headers); failure surfaced in-band |

**Why `/chat` fails open but `/ready` reports Redis failure:** they answer
different questions. `/chat` optimizes for *user-facing availability* — a brief
Redis blip should degrade budget enforcement, not take the product down.
`/ready` optimizes for *operational visibility* — orchestrators and dashboards
must know Redis is down so alerts fire and traffic can be shaped, even while
`/chat` keeps serving.

## Decisions & Trade-offs

### What I chose and why

- **FastAPI + async** — native async streaming and dependency injection fit an I/O-bound gateway; thin routes, explicit services.
- **Redis for budgets** — shared across instances (process-local counters break with multiple workers), atomic primitives, cheap TTLs for daily reset.
- **Atomic rate limiting via MULTI/EXEC + `EXPIRE NX`** — `INCR` is atomic so concurrent requests get distinct ordered counts; the transaction executes increment and first-write TTL together server-side (no crash window); `EXPIRE NX` sets the TTL only on creation and self-heals a missing TTL on the next request. A Lua script is the textbook alternative and equally atomic, but adds a scripting/testing dependency without a meaningful correctness gain here.
- **Fail-open on Redis** — availability over temporary spend enforcement, with loud logs and `/ready` visibility. Documented trade-off, not an accident.
- **LiteLLM abstraction** — isolates provider quirks to one module; swapping model/provider is a config change.
- **SSE over WebSockets** — one-way server→client streaming; trivial `curl`/browser support; far less complexity than bidirectional WebSockets for this use case.
- **Two-layer timeout** — total-operation deadline *and* per-chunk idle timeout, so a stream that starts then hangs is still bounded. Guarantees the user never waits forever; does **not** guarantee a complete answer.
- **Budget consumed before the LLM call** — failed LLM requests still count, preventing provider failure from becoming a free retry storm; keeps accounting simple (no rollback).
- **Lightweight token estimation (`chars/4`)** — provider-agnostic, no tokenizer dependency. It is explicitly an *estimate*; real billing should use provider-reported usage.
- **Structured stdlib JSON logging** — one JSON object per line, ingestible anywhere, no heavy telemetry stack.
- **Focused tests with fakes** — critical paths only; fast, deterministic, zero API cost.

### What I deliberately left out

- **Auth** — no authenticated user model in scope; `user_id` is caller-supplied. In production `/chat` would sit behind the platform's auth and operational endpoints would be network-restricted. Called out as a known limitation.
- **Persistent usage DB, Prometheus/Grafana, OpenTelemetry, semantic caching, circuit breaker, multi-provider routing, token-level billing** — each adds real operational surface without changing the core correctness story the assignment tests. All are listed under "10x time" below.

### What I would do with 10x the time

Circuit breaker around the provider; automatic provider/model fallback; exact
provider usage/cost accounting; OpenTelemetry tracing + Prometheus metrics;
load-testing harness; stronger abuse controls and distributed rate-limit tests;
idempotency keys; connection/backpressure limits; graceful shutdown that drains
active streams; and a fail-closed emergency mode for prolonged Redis outages.

## Three Failure Modes I'm Most Worried About

1. **LLM provider degradation → many long-lived streaming connections.**
   *Impact:* connections pile up, memory/FD pressure, cascading slowness.
   *Mitigation now:* total + per-chunk timeouts bound each stream.
   *Remaining risk:* no global concurrency cap.
   *Next:* per-instance max in-flight streams, load shedding, circuit breaker.

2. **Redis degradation → budget enforcement disabled → uncontrolled LLM spend.**
   *Impact:* fail-open removes the spend guard during an outage.
   *Mitigation now:* short outages expected; degradation is logged and `/ready` flips to `503` for alerting.
   *Remaining risk:* a long outage means unbounded spend.
   *Next:* alerts on `budget_check_degraded`, a global provider budget cap, and a configurable fail-closed mode for prolonged incidents.

3. **Slow/disconnected clients → backpressure.**
   *Impact:* server resources held by clients that can't consume the stream; upstream LLM work continues.
   *Mitigation now:* the final log fires in `finally`, so we always record the lifecycle.
   *Remaining risk:* upstream LLM call is not cancelled on client disconnect.
   *Next:* detect disconnect and cancel the upstream call; connection limits and idle reaping.

## Scaling to 10 Lakh Users

The take-home implementation is intentionally not "enough" on its own. It scales
by staying stateless:

- **Stateless FastAPI instances** behind a load balancer; scale horizontally. Budget state lives in Redis, so no instance affinity is needed and process-local counters (which would break across workers) are never used.
- **Redis**: managed/clustered Redis, connection pooling (already app-scoped, one pool per process — not per request), and read/latency SLAs; shard by key if needed.
- **Concurrency & cost**: per-instance in-flight stream limits, load shedding under pressure, global provider quota/budget caps, circuit breaker + provider fallback.
- **Observability**: asynchronous log/metric export, distributed tracing (OpenTelemetry), and exact usage/cost accounting from provider-reported tokens.
- **Regional**: multi-region API with regional Redis where latency/compliance justify it.

## Known Limitations

- No auth; `user_id` is trusted from the request body.
- Upstream LLM call is not cancelled on client disconnect.
- Token counts are estimates, not provider-reported usage.
- Fail-open means no spend enforcement during a Redis outage (by design).
- Concurrency is verified against the Redis primitive; a full multi-instance integration test is out of timebox scope.

## Deployment

The service is a stateless, non-root container that binds `0.0.0.0:$PORT` and
uses `/health` as its healthcheck, so it deploys unchanged to any container host.
Redis is configured entirely through `REDIS_URL` (no code change for TLS/auth).

`railway.json` targets Railway (the stack in the brief). The live instance is
hosted on **Render (free web service) + Upstash (free Redis)** because managed
Redis add-ons are paid — all three deploy from the same `Dockerfile` with no code
changes, which is the point of treating infra as configuration.

### Option A — Railway (config included: `railway.json`)

1. **Push to GitHub**, then in Railway: **New Project → Deploy from GitHub repo**.
2. **Add Redis**: project → **New → Database → Add Redis** (exposes `REDIS_URL`).
3. Gateway service → **Variables**:
   ```
   APP_ENV=production
   LLM_API_KEY=sk-or-...
   LLM_MODEL=openrouter/openai/gpt-4o-mini
   DAILY_REQUEST_LIMIT=20
   REDIS_URL=${{Redis.REDIS_URL}}
   ```
   Do **not** set `PORT` — Railway injects it and the app reads `$PORT`.
4. **Deploy**, then generate a domain under **Settings → Networking**.

### Option B — Render + Upstash (free tier, used for the live demo)

1. **Redis → Upstash** ([upstash.com](https://upstash.com)): create a free database and copy its **`rediss://...`** URL. (No code change — the app reads TLS URLs directly.)
2. **App → Render** ([render.com](https://render.com)): **New → Web Service** → connect this GitHub repo. Render detects the `Dockerfile`; choose runtime **Docker**, instance type **Free**. (Do **not** add Render's paid Redis add-on — Upstash covers Redis for free.)
3. **Health Check Path:** `/health`.
4. **Environment variables:**
   ```
   APP_ENV=production
   LLM_API_KEY=sk-or-...
   LLM_MODEL=openrouter/openai/gpt-4o-mini
   DAILY_REQUEST_LIMIT=20
   REDIS_URL=rediss://...            # the Upstash URL
   ```
   Do **not** set `PORT` — Render injects it and the app reads `$PORT`.
5. **Create Web Service.** Render builds the image and gives you a `*.onrender.com` URL.

> Render free web services spin down after ~15 min idle; the first request
> cold-starts (~30–60s). Hit `/health` once to warm it before a demo.

### Verify (either platform)

```bash
BASE=https://<your-app-domain>
curl $BASE/health     # {"status":"ok"}
curl $BASE/ready      # {"status":"ready","dependencies":{"redis":"ok"}}
curl -N -X POST $BASE/chat -H 'content-type: application/json' \
  -d '{"user_id":"demo","message":"Give me 3 backend interview tips."}'
```

Then put the live URL in the [Live Deployment](#live-deployment) section above.

Notes: with `APP_ENV=production` the app **fails fast at startup** if `LLM_API_KEY`
is missing (a deliberate guardrail). Free tiers scale to zero, so the first
request after idle has a cold start — hit `/health` once to warm it before a demo.

### Redis with TLS / auth

`REDIS_URL` fully configures the connection — no code change needed:
- TLS provider (e.g. Upstash): use the `rediss://...` URL (double `s`).
- Password-protected: `redis://:PASSWORD@host:port/0`.
