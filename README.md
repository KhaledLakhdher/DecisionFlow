# DecisionFlow

**Turn your data into decisions.**

Traditional BI tells you *what happened*. DecisionFlow is an AI business analyst
that also answers *why it happened*, *what happens next*, and *what you should
do about it*.

Upload your CSVs. Thirty seconds later you have cleaned data, generated KPIs, a
dashboard, and an analyst you can interrogate in plain English.

---

## Status

Milestone 1 — in progress. See [Roadmap](#roadmap).

| Module | Area | State |
|---|---|---|
| — | Infrastructure (Postgres + pgvector, Redis, MinIO) | ✅ Running |
| — | Tenancy: orgs, users, roles, invites, RLS | ✅ Complete |
| 1 | Data ingestion (CSV/Excel → object storage → DuckDB `raw`) | ✅ Complete |
| 2 | Data engineering (profile → validate → clean → `clean` layer) | ✅ Complete |
| 3 | Warehouse (star schema) | ⬜ |
| 4 | Semantic layer + automatic KPIs | ✅ Complete |
| 5 | Machine learning (forecast, churn, anomalies) | ⬜ |
| 6 | LLM narrative layer + NL→SQL agent | ✅ Complete |
| 7 | Agent fleet | ⬜ Next |

---

## Architecture

```
                     Next.js 15 + React 19
                              │
                        FastAPI (async)
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        Agent fleet      Analytics       Ingestion
       (Gemini-driven)     engine         pipeline
              │               │               │
              └───────────────┼───────────────┘
                              │
      ┌──────────────┬────────┴────────┬──────────────┐
      │              │                 │              │
  Postgres        DuckDB            MinIO          Redis
 (control      (per-workspace     (uploaded       (ARQ job
   plane)       analytics)          files)         queue)
   + pgvector
```

### Two planes, on purpose

**Control plane — Postgres.** Identity, tenancy, dataset catalog, semantic
layer, conversations, embeddings. Small rows, high write concurrency,
transactional integrity. Postgres is exactly right for this.

**Data plane — DuckDB, one database file per workspace.** Customer analytical
data. Columnar, vectorised, and embarrassingly fast for the
`GROUP BY`-over-millions-of-rows queries a BI tool actually issues. Physical
file separation per tenant is also the strongest isolation boundary available:
one workspace's analytical queries cannot reach another's bytes, regardless of
any application bug.

### Why Polars and DuckDB instead of pandas

The ingestion pipeline uses Polars for transformation and DuckDB for storage
and query. Polars is lazy, multi-threaded, and has no index semantics to fight;
DuckDB is already the query engine, so pandas would be a third data
representation earning nothing. Avoiding it also side-steps the pandas 3.0
breaking changes.

### Tenant isolation is enforced twice

1. **Application layer** — every repository query filters on `org_id`.
2. **Postgres RLS** — policies reject rows whose `org_id` does not match the
   `app.current_org_id` setting on the connection.

The second layer exists because the first is one forgotten `.where()` away from
leaking another company's revenue figures. The API deliberately connects as
`decisionflow_app`, a role holding neither SUPERUSER nor BYPASSRLS — those
attributes make a role ignore row-level security outright, and `FORCE ROW LEVEL
SECURITY` cannot restrain them. The migration role *is* the image's bootstrap
superuser and therefore does see every row; that is expected, and is why the
runtime role is a different one. Hardening step before production: make the
schema owner an ordinary non-superuser role.

`apps/api/tests/test_rls.py` asserts all of this against a real database —
including the one that bites in practice: tenant context must survive a
`commit()`.

The setting is applied on an `after_begin` event rather than once per request:
`set_config(..., is_local := true)` is transaction-scoped and would otherwise
be silently discarded by the first `commit()`. When the setting is absent the
policy compares against NULL and matches nothing — so a missing tenant context
fails closed.

---

## Getting started

### Prerequisites

- Docker Desktop (running)
- Python 3.12+
- Node.js 20+

### 1. Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # paste into SECRET_KEY
```

Add a `GEMINI_API_KEY` from [AI Studio](https://aistudio.google.com/apikey).
Everything except the AI endpoints runs without one.

### 2. Start infrastructure

```bash
docker compose up -d
```

Brings up Postgres (with pgvector), Redis, and MinIO, and provisions the
least-privileged `decisionflow_app` role.

### 3. Run the API

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on Unix
pip install -e ".[dev]"
alembic upgrade head
uvicorn decisionflow.main:app --reload
```

API docs at http://localhost:8000/docs.

### 4. Run the worker

Scheduled maintenance and (later) the ingestion pipeline run out of process:

```bash
arq decisionflow.worker.main.WorkerSettings
```

---

## Testing

```bash
cd apps/api
pytest
```

The suite runs against a real Postgres. It provisions a dedicated
`decisionflow_test` database, applies the real migrations, and drops it
afterwards — your development data is never touched.

Real database rather than SQLite or mocks, because the things most worth
testing here *are* database behaviour: RLS policies, partial unique indexes,
enum types, and `ON DELETE` cascades. None of them exist in a mock, and a
suite that passes without them would be testing the wrong system.

Tests share one session-scoped event loop. The SQLAlchemy engine and Redis
client are module-level singletons, and with pytest-asyncio's default
function-scoped loop their pooled connections outlive the loop that created
them — the next test then dies on a closed transport.

---

## Repository layout

```
apps/
  api/                    FastAPI backend
    decisionflow/
      core/               settings, logging, security, errors
      db/                 models, session, RLS helpers
      api/v1/             HTTP endpoints
      schemas/            Pydantic request/response contracts
      services/           business logic (no HTTP imports)
      worker/             ARQ jobs and cron schedule
    tests/                pytest suite (real Postgres)
  web/                    Next.js frontend
infra/
  postgres/init/          one-time database provisioning
```

`services/` never imports `fastapi`. Business logic raises typed errors from
`core.errors`; a single handler in `main.py` renders them as HTTP. That keeps
the logic testable without a client and reusable from the worker.

### Ingestion

Upload is split across two processes, deliberately:

```
POST /datasets/upload          →  stream to MinIO, create Dataset row, return 202
        ↓ enqueue (ARQ)
worker: ingest_dataset         →  read file, detect schema, load raw.<slug>
        ↓
status: uploaded → analyzing → ready | failed
```

Parsing a 200 MB spreadsheet inside a request handler means a timeout and a
worker pinned on CPU while other requests queue behind it. Once the bytes are
in object storage the work is durable and retryable — which is why `POST
/datasets/{id}/reingest` never asks the customer to upload anything again.

The uploaded file is never modified. Everything downstream (the `raw` table,
the future `clean` table, KPIs) is derived, so a bad transform is a re-run
rather than an apology.

**Column names** are normalised to identifier-safe forms (`Total Revenue (USD)`
→ `total_revenue_usd`), with duplicates, blanks, leading digits and SQL
keywords all handled. The original is kept on the model — it carries meaning
for humans and for the LLM that the normalised form loses. Renaming happens in
Polars, *before* anything reaches SQL, so no customer-supplied text is ever
interpolated into DDL.

**DuckDB concurrency** is the sharp edge here. DuckDB allows either one
read-write process or several read-only ones, never both — and the API reads
while the worker writes. Postgres advisory locks arbitrate: writers take an
exclusive lock, readers a shared one, keyed on the workspace. The
transaction-scoped variants (`pg_advisory_xact_lock`) are used so a crashed
worker cannot strand a workspace behind a lock it never releases.

### Cleaning

```
profile(raw) → plan → clean → profile(clean) → validate → persist
```

Runs entirely against tables already in DuckDB, never the uploaded file — so
re-cleaning with different settings costs one SQL pass, not a re-download and
re-parse. `raw` is never modified; it is the reproducible starting point.

**Deciding and doing are separate.** `plan_cleaning` reads a profile and
returns typed actions, touching no data. `build_clean_sql` compiles that plan
into a single `CREATE TABLE ... AS SELECT`, vectorised across every row. The
plan doubles as the audit trail: a BI tool that silently rewrites a customer's
numbers is worse than one that does nothing, so every change is recorded and
shown back.

What it recovers, automatically:

| Input | Output |
|---|---|
| `"$1,249.99"` (text) | `1249.99` (decimal) |
| `"N/A"`, `"-"`, `""` | `NULL` — never `0` |
| `"yes"` / `"no"` | `true` / `false` |
| `"  Ada Lovelace  "` | `"Ada Lovelace"` |
| exact duplicate rows | removed, and counted |

Coercion only applies above a 95% threshold. Below it, converting would turn
the minority into NULLs — destroying data to tidy a column, which is not a
trade worth making automatically. Such columns are flagged as `mixed_types`
instead.

Placeholders are excluded from that threshold *before* it is measured. A
revenue column of `"$1,249.99"` values with a single `"N/A"` is a numeric
column with one missing value, and counting the placeholder as "not a number"
is exactly what would stop it being recognised.

**Findings come from the raw profile; stored statistics come from the clean
one.** The user should see the problems that were *found*, while analysis and
the LLM should read the table they will actually query.

### The semantic layer

Generating KPIs without configuration requires answering a question the schema
cannot: *is this number something to add up, or an identifier that happens to
be numeric?* `order_id` and `revenue` are both integers to a database, and
summing the first produces a confident, meaningless figure that nothing
downstream would catch.

Two signals, combined in a specific order:

- **Statistics** decide the **role** — cardinality and type separate a category
  from a key, but cannot tell revenue from quantity.
- **Name patterns** decide the **tag** — `revenue`, `qty`, `unit_cost` carry
  intent that statistics cannot see.

Doing it the other way round means a column called `total_orders_id` gets
summed. Roles are *stored* on the column and exposed via
`GET /datasets/{id}/semantics`, because the heuristics will sometimes be wrong
and a user cannot correct what they cannot see.

One caveat worth knowing: the distinct-*fraction* test is skipped below 50
rows. Three regions across six rows reads as a 0.5 unique fraction — identical
to a genuinely unique column in a larger table — so absolute cardinality is the
reliable signal on small samples.

### Automatic KPIs

Metrics are emitted **only when the columns they need exist**. Revenue needs a
monetary measure; AOV additionally needs an order key; margin needs a cost.
A dataset supporting none of them returns none, rather than a dashboard of
zeroes — inventing a metric the data cannot express is worse than an absent
tile.

From a plain sales CSV, with no configuration:

```
Total revenue      $5,900.00   +5.3% vs previous month
Average order value  $983.33
Gross margin           46.6%
Repeat customer rate   50.0%
Revenue per customer $1,475.00
Unique customers           4
```

**Every KPI stores the SQL that produced it.** That is not decoration: it is
how a user checks a number they distrust, and how the narrative layer explains
a figure without re-deriving it. Values are `NUMERIC`, never float — a revenue
total that disagrees with the customer's own books by a cent is a support
ticket.

Growth uses a window function over aggregated periods (aggregate first, then
`lag`), which stays correct when a period is missing entirely — a
date-arithmetic join silently gets that wrong.

### The analyst agent

```
question → SQL → guard → execute → narrate
```

**Two model calls, not one.** Asking a model to write SQL *and* describe the
results in a single turn means it narrates results it has never seen — which is
precisely how a plausible, fabricated number reaches an executive. The second
call receives the real rows and has nothing else to invent from.

**The model is allowed to refuse.** When the columns cannot support a question,
declining is the correct answer and is returned as `answerable: false` — not an
error. A confident query over the wrong column is far worse than "this data
can't tell you that."

**Rejections drive retries.** When generated SQL fails the guard or the engine,
the *specific* reason goes back to the model. "COPY is not permitted" yields a
fixed query; "invalid query" yields the same query again.

Every answer stores the SQL that produced it. An AI-produced figure without
visible provenance should not be trusted, and this is what makes it checkable.

### SQL safety

The model is untrusted input — steered by column names and free-text questions
nobody controls. Prompt instructions are not a security control. Three
independent layers, each verified against DuckDB 1.5 rather than assumed:

| Layer | Stops |
|---|---|
| `enable_external_access=false` | `read_csv`, `COPY TO`, `ATTACH`, `INSTALL`, `glob` |
| DuckDB's own parser | statement chaining, DDL, DML |
| Textual denylist | the same functions, earlier and with a better message |

The first is load-bearing, and worth stating plainly because it is easy to get
wrong: **`read_only=True` does not prevent file access.** A read-only DuckDB
connection will happily `read_csv('/etc/passwd')` or write a file to disk —
read-only protects the *database's contents*, not the host. This was verified
by attempting each attack against a read-only connection before the guard
existed.

`enable_external_access` is also a **one-way latch**: a generated
`SET enable_external_access=true` fails with "Cannot enable external access
while database is running", so the model cannot undo it.

Cross-tenant access is structurally impossible regardless — each workspace has
its own DuckDB file, so there is no other tenant's data inside the database
being queried.

Queries additionally carry a forced row cap and a wall-clock timeout, because
one generated cross join would otherwise pin a worker thread indefinitely.

### Authorization

Two levels of caller identity, deliberately distinct:

- **`Principal`** comes from the access token alone and costs no query. It
  answers "who is this?" — enough for endpoints not scoped to a workspace.
- **`TenantPrincipal`** additionally proves against the database that the
  caller is *still* a member of the workspace, and carries their **live** role.

Workspace endpoints use the second. The token's `org` and `role` claims are a
statement of intent, never authority — otherwise removing someone from a
workspace, or demoting them, would leave them holding working credentials until
their token expired. The check is one indexed read on a unique key, sharing the
request's existing session.

---

## Roadmap

**Milestone 1 — vertical slice.** Upload a CSV, get it profiled and cleaned,
see generated KPIs on a dashboard, and ask one question in natural language
that is answered from your actual data.

**Milestone 2 — the analyst.** Star-schema modelling, forecasting, churn
classification, anomaly detection, narrative explanation.

**Milestone 3 — the agent fleet.** Specialised agents (SQL, visualisation,
forecast, report, recommendation) behind an executive agent that decomposes a
CEO-level question and routes it.

---

## Licence

Not yet determined.
