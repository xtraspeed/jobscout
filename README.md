# JobScout

**An async, production-shaped web scraping platform that turns public job listings into a queryable dataset.**

Crawls public job postings, normalises them into PostgreSQL, and serves them
through a REST API and a Streamlit dashboard. Built around the parts of scraping
that are usually skipped: politeness, idempotency, change detection, observability,
and the ability to re-parse historical data when a site changes.

```text
adapters ──► polite async fetcher ──► gzip archive ──► parsers ──► PostgreSQL ──► API ──► dashboard
 robots.txt     token bucket          raw_pages        selectolax    upsert-on-hash  FastAPI   Streamlit
 circuit break  Retry-After          re-parseable     PII scrub     keyset paging
```

---

## Why this is more than a `requests` loop

Most scraping projects fail on the boring parts. This one is built around them:

| Problem | How it is handled here |
|---|---|
| You get blocked or banned | `robots.txt` enforcement, per-domain token bucket, `Crawl-delay` support, `Retry-After` obedience, circuit breaker per host |
| Re-running duplicates everything | `content_hash` change detection with `INSERT ... ON CONFLICT DO UPDATE ... WHERE hash differs` — a re-crawl of unchanged data writes almost nothing |
| The site redesigns and your scraper breaks | Every response is gzipped into `raw_pages`; fix the selectors and `jobscout reparse` rebuilds the dataset **with zero requests to the site** |
| Pages arrive faster than you can store them | Bounded-concurrency fan-out over a deduplicating frontier, with batched writes |
| One bad page kills the run | Per-page error isolation, recorded in `fetch_errors`, counted per run |
| You cannot tell if it is working | Prometheus metrics, structured JSON logs, per-run counters, and a dashboard page that shows whether the last crawl worked |
| Nobody can reproduce your results | The test suite runs the *real* crawler, the *real* parsers and the *real* pipeline against recorded fixtures — fully offline |

---

## Quick start (offline, ~60 seconds)

No network, no Docker, no PostgreSQL. This runs the entire pipeline against
recorded fixtures and prints what it found.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head

# Crawl the recorded fixture board
jobscout crawl --target fixture --path tests/fixtures/demo_board

# Crawl it again: nothing new, nothing changed
jobscout crawl --target fixture --path tests/fixtures/demo_board

# Re-parse the archive without any network access
jobscout reparse --target fixture --path tests/fixtures/demo_board

# Look at the data
jobscout stats
```

Then, in three terminals:

```bash
jobscout serve          # API on http://localhost:8000/docs
make dashboard          # Streamlit on http://localhost:8501
```

Or the whole stack in containers:

```bash
docker compose up --build --wait
python scripts/smoke_test.py
```

---

## How a crawl works

```text
adapter.start()          seed URLs
        │
        ▼
   ┌─────────┐   pop up to N, bounded by global_concurrency
   │ Frontier │   deduplicated by canonical URL, depth-limited
   └─────────┘
        │  asyncio.gather
        ▼
   ┌──────────────────────┐  robots.txt → circuit breaker → token bucket
   │    HttpFetcher       │  retry w/ backoff → snapshot to raw_pages
   └──────────────────────┘
        │
        ▼
   adapter.parse()        → Follow (more URLs)  |  Emitted (a JobItem)
        │
        ▼
   JobRepository.upsert_many()   new / changed / unchanged, batched
        │
        ▼
   mark_stale()           listings missing from this run are flagged inactive
```

Two different limits are at work, and the distinction matters:

- **global concurrency** bounds pages in flight — this protects *our* memory and connection pool.
- **the per-domain limiter** bounds the request rate — this protects the *site*.

One `httpx.AsyncClient` (HTTP/2, keep-alive, connection pooling) is shared by the
whole run. Creating a client per request is the single most common performance
mistake in naive scrapers.

---

## Targets

| Adapter | What it does | Notes |
|---|---|---|
| `hnhiring` | Hacker News "Ask HN: Who is hiring?" threads | Public Algolia + Firebase APIs. Three-stage crawl: search → thread → comments. Parses free-form company text into structured postings. |
| `htmlboard` | Any conventional job board | **Config, not code.** Selectors live in YAML. |
| `fixture` | Recorded pages from disk | Runs the real parsers over recorded HTML. Used by the tests, CI and the offline demo. |

### Adding a new board

Copy a selector file and change the selectors. No Python:

```yaml
# my_board.yml
name: my_board
start_urls: ["https://board.example/jobs"]
list:
  item_selector: "div.job-card"
  url: "a.job-title::attr(href)"
  fields:
    title: "h2.job-title::text"
    company: "span.company::text"
    tags: "ul.tags li::text"       # multi-value fields are split on , | ; ·
detail:
  fields:
    title: "h1::text"              # repeat key fields so pages parse standalone
    description: "div.description::html"
pagination:
  next_selector: "a.next"
```

```bash
jobscout crawl --target htmlboard --config my_board.yml --allow-network
```

Selector syntax is `<css>::text`, `<css>::html` or `<css>::attr(name)`.

---

## Re-parsing: the payoff for storing raw pages

This is the feature that separates a scraper you maintain from a scraper you
rewrite. When a board changes its markup:

```bash
# 1. fix the selectors in my_board.yml
# 2. rebuild the dataset from the archive — no requests to the site at all
jobscout reparse --target htmlboard --config my_board.yml
```

`reparse` replays every archived body through the current parser and updates only
the rows whose content actually changed. In the test suite this is covered by
`tests/test_reparse.py`, including a check that no HTTP client is even
constructed during a re-parse.

---

## The API

Read-only, cursor-paginated, filterable. Interactive docs at `/docs`.

```text
GET  /items              filters: q, company, location, remote, employment_type,
                         source, posted_after, min_salary, has_salary, sort, limit, cursor
GET  /items/{id}         full description and provenance
GET  /companies          aggregated by company, with remote counts
GET  /facets/{field}     distinct values, for populating filter controls
GET  /stats              totals plus a dense daily series
GET  /runs               crawl history
GET  /runs/{id}          one run, with a failure breakdown
GET  /health /livez      liveness, no dependencies touched
GET  /ready /readyz      readiness, requires PostgreSQL
GET  /metrics            Prometheus exposition
```

Pagination is **keyset**, not offset: a 200k-row table pages consistently while
rows are being inserted.

Full-text search uses PostgreSQL `tsvector` with a `'simple'` configuration —
deliberately not English-stemmed, because job titles are full of proper nouns and
identifiers (`C++`, `Node.js`, `M4`) that stemming mangles. SQLite falls back to
substring matching so the test suite needs no server.

---

## Dashboard

Streamlit, four pages, talking to the API over HTTP (never to the database).

- **Overview** — headline metrics, listings-per-day trend, source breakdown (Altair)
- **Listings** — sidebar filters, full-text search, selectable table, row detail
- **Companies** — rollup table and a "most advertised" chart
- **Crawl runs** — run history, status, counters, breaker state

Caching is deliberate: Streamlit reruns the whole script on every widget change,
so API calls are `@st.cache_data(ttl=...)`-wrapped and the HTTP client is a
`@st.cache_resource` singleton. Pages are covered headlessly in CI with
`streamlit.testing.v1.AppTest`.

---

## Ethics and compliance

This is the part most scraping projects skip, and it is the part that matters.

- `robots.txt` is fetched, parsed and obeyed by default. `Crawl-delay` is fed to
  the rate limiter rather than merely parsed.
- If `robots.txt` cannot be fetched (5xx, timeout) the crawler **fails closed**:
  it skips the host rather than hammering a site that is already struggling.
- A real, contactable `User-Agent` is required. Set `JOBSCOUT_USER_AGENT` with an
  address a human can answer.
- Live crawling is **opt-in**: `jobscout crawl` refuses to touch the internet
  without `--allow-network`.
- Only company-level public postings are kept. Emails, phone numbers and long
  numeric identifiers are scrubbed at parse time and never reach the database.
- Rate defaults are conservative (1 req/s per host, 2 concurrent). Raise them only
  if the site's terms allow it.
- Before pointing this at a real site, read its `robots.txt` **and** its terms of
  service. That is your responsibility, not the library's.

---

## Testing

```bash
make test          # 265 unit tests, fully offline
make integration   # 17 tests, needs JOBSCOUT_TEST_DATABASE_URL
make cov           # with coverage
make lint typecheck
```

**265 unit tests, no network access required.** Highlights:

- Politeness primitives tested on an **injected clock**, so rate limiting is
  verified without real waiting
- Retry/backoff, `Retry-After` parsing and the circuit-breaker state machine
- Parsers tested against recorded HTML, including the awkward cases (ad slots
  that must be ignored, PII that must be redacted, inverted salary ranges,
  day-first vs month-first dates)
- **Idempotency**: a second crawl must report every item as unchanged and add no rows
- **Schema drift detection**: `tests/test_migrations.py` runs the real Alembic
  migrations and compares the result against the ORM metadata
- **Dialect verification**: the PostgreSQL-only SQL is *compiled* and asserted
  (`tests/test_postgres_sql.py`), so `ON CONFLICT`, the `tsvector` search and
  `date_trunc` are checked even though the suite runs on SQLite
- **Offline replay**: the fixture adapter drives the real `HttpFetcher` through a
  fixture transport, so politeness, retries, archiving and metrics all execute
- **Streamlit UI tests** via `AppTest`, including the navigation shell

### Integration tests

The unit suite runs on SQLite, which means the PostgreSQL-only behaviour is never
executed. `tests/test_integration.py` covers exactly that gap and needs a
disposable database:

```bash
export JOBSCOUT_TEST_DATABASE_URL=postgresql+asyncpg://jobscout:jobscout@localhost:5432/jobscout_test
make integration
```

It drops and recreates the `public` schema, so **never point it at a database you
care about**. It asserts `ON CONFLICT` semantics, `JSONB` round-trips, real
`tsvector` search (including that the `simple` dictionary does not stem job
titles), `date_trunc` bucketing, timezone-aware timestamps, archive round-trips,
transaction rollback, and that **eight concurrent writers to the same natural key
produce exactly one row**.

Those tests skip cleanly when no database is configured, so `make test` stays
hermetic. In CI they run against the `postgres:16-alpine` service.

---

## Project layout

```text
src/jobscout/
  config.py            typed settings (pydantic-settings, JOBSCOUT_ prefix)
  models.py            JobItem, FetchedPage, Follow, Emitted
  fetch/               pooled client, token bucket, robots, retry, breaker, snapshots
  adapters/            hnhiring, htmlboard, fixture + selectors/*.yml
  parse/               selectolax helpers, salary/date parsing, PII scrubbing, board config
  pipeline/            frontier, crawler loop, run orchestration, re-parse
  store/               SQLAlchemy models, async repositories, upsert-on-hash-change
  api/                 FastAPI query service
  dashboard/           Streamlit app (client + pages)
  observability/       structlog, Prometheus metrics
  cli.py               command-line interface
alembic/               migrations (migration-only; no create_all at runtime)
tests/                 265 unit + 17 integration tests, recorded fixture bundles
scripts/               smoke_test.py, record_fixtures.py
ops/prometheus/        scrape config
```

Design decisions and their reasoning: [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Configuration

Every setting is an environment variable with a `JOBSCOUT_` prefix. See
[`.env.example`](.env.example).

| Variable | Default | Why you would change it |
|---|---|---|
| `JOBSCOUT_DATABASE_URL` | `sqlite+aiosqlite:///./jobscout.db` | PostgreSQL in production |
| `JOBSCOUT_USER_AGENT` | `JobScoutBot/0.1` | **Set this to something contactable** |
| `JOBSCOUT_RESPECT_ROBOTS` | `true` | Leave it on |
| `JOBSCOUT_PER_DOMAIN_RATE` | `1.0` | Lower for a fragile site |
| `JOBSCOUT_PER_DOMAIN_CONCURRENCY` | `2` | Raise only if the site allows it |
| `JOBSCOUT_MAX_RETRIES` | `4` | Fewer retries for a large crawl |
| `JOBSCOUT_STORE_SNAPSHOTS` | `true` | Turning this off disables `reparse` |
| `JOBSCOUT_SNAPSHOT_MAX_BYTES` | `4 MiB` | Cap archive growth |
| `JOBSCOUT_MAX_PAGES` | `200` | Per-run page budget |
| `JOBSCOUT_LOG_FORMAT` | `json` | `console` for local reading |

---

## Deployment notes

- **Migrations run as a one-shot job**, never from API or crawler startup.
  Concurrent replicas racing to migrate is a classic way to take down a database.
  In `compose.yaml` every service waits for `migrate` to complete successfully.
- **Connection capacity** is `replicas x processes x (pool_size + max_overflow)`.
  Size those against the PostgreSQL `max_connections` budget rather than
  choosing them generously.
- **The container runs as UID 10001** and the API needs no write access to the
  image.
- **Database data belongs in a managed volume**, never in the container filesystem.
- `/metrics` should be restricted to internal networks or gated at the ingress.

---

## Known limitations

Stated plainly, because a project that admits its edges is easier to trust:

- **No JavaScript rendering.** Pages that require a browser are not handled; the
  extension point is a Playwright-backed `Fetcher` transport.
- **The frontier is in-memory.** Fine for one run; a resumable multi-day crawl
  would need it persisted. Cross-run idempotency already comes from the
  `(source, external_id)` unique key, so persistence is not needed for correctness.
- **The integration tests have not been executed against a live PostgreSQL in
  this repo's current environment** (no Docker or server available). They are
  written to run in CI against the `postgres:16-alpine` service; until that has
  run green, treat the PostgreSQL behaviour as reviewed-but-unproven. The
  dialect-specific SQL is separately *compiled and asserted* in
  `tests/test_postgres_sql.py`, which does run everywhere.
- **No JS-rendered infinite scroll.** Pagination strategies are next-link or a
  page template.
- **The archive is a debugging tool, not an audit log.** `prune_snapshots` keeps
  the newest body per URL; it does not retain history.
- **Schema drift is caught in tests, not in production.** `alembic check` should
  be added to a deploy step if you want that gate.

---

## License

MIT.
