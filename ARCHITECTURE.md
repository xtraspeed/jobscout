# Architecture

Design decisions and the reasoning behind them. The [README](README.md) covers
what the project does; this covers why it is built this way.

---

## 1. Layering

```text
cli.py
  │
  ├── pipeline/        orchestration: frontier -> fetch -> parse -> upsert
  │     ├── frontier.py    deduplicating, depth-limited work queue
  │     ├── crawler.py     the crawl loop
  │     └── runner.py      database lifecycle; offline re-parse
  │
  ├── adapters/        the ONLY site-specific code
  │     ├── base.py       the Adapter protocol
  │     ├── hnhiring.py   HN hiring threads (JSON inside HTML inside JSON)
  │     ├── htmlboard.py  config-driven HTML boards
  │     ├── fixture.py    offline replay through the real parsers
  │     └── selectors/    YAML configs, shipped inside the package
  │
  ├── fetch/           transport-agnostic HTTP with policy
  │     ├── client.py    pooled httpx client; robots, breaker, retry, archive
  │     ├── ratelimit.py token bucket + per-host concurrency semaphore
  │     ├── robots.py    robots.txt cache and verdicts
  │     ├── retry.py     retry classification and backoff
  │     ├── breaker.py   per-host circuit breaker
  │     ├── snapshot.py  archive protocol + in-memory implementation
  │     └── transport.py httpx transports that serve recorded data
  │
  ├── parse/           pure functions, no I/O
  │     ├── html.py      selector mini-language, defensive extraction
  │     ├── text.py      salary/date parsing, PII scrubbing
  │     ├── board.py     config-driven job board parser
  │     └── hn.py        HN comment parser
  │
  ├── store/           all SQL lives here
  │     ├── models.py    SQLAlchemy tables
  │     ├── db.py        engine, sessions, dialect detection
  │     ├── repositories.py  queries, filters, keyset pagination
  │     └── snapshots.py archive persistence + pruning
  │
  ├── api/             FastAPI query service (read-only)
  ├── dashboard/       Streamlit UI (talks to the API over HTTP)
  └── observability/   structlog + Prometheus
```

**The dependency rule:** nothing below `store/` imports anything above it.
`fetch/` never imports `store/` (the archive is injected as a protocol), and
`parse/` imports neither. That is what lets each layer be tested in isolation, and
it is why the fixture adapter can drive the real crawler without a database.

---

## 2. The adapter contract

```python
class Adapter(Protocol):
    name: str
    async def start(self) -> Sequence[Follow]: ...
    def parse(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]: ...
```

Two decisions worth explaining:

**Adapters return data, they do not perform I/O.** An adapter is handed a page
that has already been fetched politely, and returns either more URLs to visit or
finished items. This means:

- the same parser runs over live HTTP and over recorded fixtures;
- the crawl loop is identical for every target, so politeness, retries and
  persistence cannot be re-implemented per site;
- an adapter is trivially unit-testable with a hand-built `FetchedPage`.

**`parse` receives the `Follow` that produced the page.** Without it, an adapter
would have to sniff the URL to work out whether it is looking at an index page or
a detail page. Passing `follow.kind` and `follow.meta` makes that explicit, and
the same information is what the archive stores so a re-parse can replay a page
with the same context.

**Invariant: `adapter.name` must equal the `source` stamped on its items.** The
pipeline scopes staleness marking, run rows and metric labels by adapter name. An
early version of `FixtureAdapter` suffixed its name with `-fixture` for
convenience, which silently broke `mark_stale` — caught by a test, and now stated
as an invariant in the code.

---

## 3. Politeness as a first-class concern

`robots.txt`, rate limiting, backoff and a circuit breaker are not optional extras
bolted onto a fetcher; they are the fetcher.

### Why a token bucket, and not a sleep

A fixed `sleep()` between requests is wrong in both directions: it wastes
bandwidth when a host is fast, and it cannot express a burst allowance. A token
bucket with `rate = requests/second` and `capacity = rate` permits a natural burst
and then smooths traffic to the configured rate.

The clock and the sleep function are **injected**, which is why the rate-limiting
tests assert real timing behaviour in milliseconds instead of sleeping for them.

### Why a circuit breaker

If one host starts timing out, a naive crawler keeps retrying it inside the global
concurrency budget and stalls on work it could be doing elsewhere. After
`circuit_failure_threshold` consecutive failures the breaker opens and requests to
that host are skipped immediately; after a cool-off it admits a single trial
request to test recovery. Breakers are per host and half-open state admits exactly
one trial, so a recovering host is not stampeded.

### Why fail closed on a robots.txt error

If `robots.txt` returns 5xx or the connection fails, the crawler refuses the host.
The alternative — assuming permission — means hammering a site that is already in
trouble, which is exactly the situation politeness exists for. `4xx` is different:
a missing `robots.txt` means no restrictions were published, so everything is
allowed.

### Why `Retry-After` overrides our backoff

A `Retry-After` header is a server instruction, not a hint. Honouring it — and
skipping our own exponential backoff entirely — is both more polite and simpler to
reason about.

---

## 4. Change detection and idempotency

The single most valuable property of this system: **re-crawling unchanged data
changes nothing.**

```python
INSERT INTO job_items (...) VALUES (...)
ON CONFLICT (source, external_id) DO UPDATE SET
    title = excluded.title, ..., last_seen_at = excluded.last_seen_at
WHERE job_items.content_hash <> excluded.content_hash
```

- `content_hash` is a SHA-256 over the user-visible fields only. Timestamps and
  `raw` are excluded, so re-fetching a page that has not meaningfully changed is a
  no-op rather than churn.
- `first_seen_at` is never updated; `last_seen_at` always is. That pair is what
  makes "is this still advertised?" answerable.
- `mark_stale` flags listings that a run did *not* see, rather than deleting them.
  Deleting would destroy history; flagging keeps the dataset queryable and makes
  the disappearance visible.
- The natural key is `(source, external_id)`, so the same job id from two different
  boards is two rows, not a collision.

The write classification (new / changed / unchanged) is computed from one `SELECT`
of stored hashes followed by one bulk upsert — two round trips per batch, regardless
of batch size.

---

## 5. Storing raw responses

Every fetched body is gzipped into `raw_pages` with its URL, status, headers,
content hash, and the request `kind`.

This costs disk and buys three things:

1. **Re-parsing.** Fix a selector, rebuild the dataset, zero requests to the site.
2. **Debugging.** A parser bug can be reproduced against the exact bytes that
   caused it, weeks later.
3. **Graceful degradation.** When `robots.txt` is unavailable or a page 404s after
   a successful crawl, the previous body is still available.

The archive is a debugging and re-parsing tool, not an audit log:
`prune_snapshots` keeps the newest body per URL using a window function, which
behaves identically on PostgreSQL and SQLite.

Archiving never fails a crawl. `_archive` catches everything and logs a warning,
because losing a debug copy is not a reason to lose the listing.

---

## 6. Concurrency: two different limits

```text
global_concurrency   ->  how many pages are in flight  -> protects OUR resources
per_domain_rate      ->  how fast we hit one host       -> protects THE SITE
per_domain_concurrency -> how many requests to one host at once
```

The crawl loop pops up to `global_concurrency` items from the frontier and runs
them with `asyncio.gather`, then pushes whatever they discovered. The per-domain
limiter is what actually protects a target site, and it applies inside each fetch
regardless of how much global concurrency is available.

**Why a depth-first heap rather than a work-stealing scheduler?** A heap gives
deterministic, testable crawl order and natural depth limiting. For job boards —
small, bounded, mostly two levels deep — a priority queue is the right amount of
machinery. A genuinely large crawl would want per-host queues so one slow host
cannot occupy the whole global budget, which is the natural next step.

**The frontier is in-memory and deduplicated by canonical URL** (fragment dropped,
query parameters sorted). That is deliberate: cross-run idempotency already comes
from the database unique key, so persisting every visited URL would add write
amplification for no correctness gain.

---

## 7. Portability: PostgreSQL and SQLite from one schema

The entire test suite runs on a temporary SQLite file. That is only possible if
the schema and queries avoid PostgreSQL-only constructs, so:

- JSON columns use the portable `JSON` type, not `JSONB`;
- timestamps are written from Python (`datetime.now(timezone.utc)`) rather than
  `now()`, so both backends agree;
- upserts use the dialect's `insert().on_conflict_do_update()`, selected by name;
- full-text search is the one place that genuinely differs:

  | Backend | Search |
  |---|---|
  | PostgreSQL | `to_tsvector('simple', search_text) @@ plainto_tsquery('simple', q)`, GIN index |
  | SQLite | `ILIKE` on `search_text` / `title` / `company` |

The GIN index is an **expression** index created only in the migration, behind a
dialect check, so the table definition itself is identical everywhere.

**SQLite uses `NullPool`.** A pooled `aiosqlite` connection is bound to the event
loop that opened it, which breaks as soon as the test client opens a new loop per
request. `NullPool` sidesteps that and costs nothing for SQLite.

**Slash dates are read as US `M/D/Y`** unless the first component exceeds 12, in
which case the value is unambiguous `D/M/Y`. Documented, tested, and chosen
because scraped English-language text follows US convention more often than not.

### Verifying the branch that never runs

Running everything on SQLite means the PostgreSQL statements are never executed.
Two mechanisms close that gap without a server in the loop:

1. **The dialect-specific SQL is built by pure, session-free functions**
   (`build_upsert_statement`, `build_search_condition`, `build_day_bucket`).
   The repository calls them, and `tests/test_postgres_sql.py` calls the same
   functions and compiles the result for the PostgreSQL dialect. A misspelled
   function, a wrong `excluded` reference, or an accidentally dropped
   `WHERE job_items.content_hash != excluded.content_hash` fails there rather
   than in production.
2. **`tests/test_integration.py`** runs the real statements against a real
   server, including the concurrency case where eight writers race on one
   natural key. It is marked `integration`, skips without a configured database,
   and runs in CI.

Extracting those builders was a design change, not a test scaffold: it is what
makes the two dialects symmetrical and reviewable side by side.

---

## 8. Schema management

Migrations only. Nothing in the application calls `Base.metadata.create_all`.

`tests/test_migrations.py` runs the real Alembic migrations against a temporary
SQLite database and compares the result against the ORM metadata: table set,
columns, primary keys, unique constraints and indexes. This exists because the
ORM models and the migrations are two independent declarations of one schema and
nothing else forces them to agree.

It has already paid for itself: the migration originally declared `raw_pages.id` as
a plain `BIGINT`, which passes every structural assertion and then fails on the
first insert under SQLite, because only an `INTEGER PRIMARY KEY` is a rowid
alias. The drift test asserts the auto-increment behaviour explicitly.

---

## 9. Observability

Metrics are labelled with bounded-cardinality values only:

- adapter name, host, route **template** (`/items/{item_id}`), status, outcome
- never a raw URL, request id, company name or destination

`/items/{code}`-style templates are used in logs and metrics precisely so a
crawl cannot explode Prometheus cardinality. Query patterns are logged
structurally by structlog, with the JSON renderer in containers.

The `crawl_runs` table is the durable counterpart: per-run counters, breaker
state, duplicates skipped, robots refusals and bytes downloaded, all queryable
through `/runs` and rendered on the dashboard.

---

## 10. The dashboard is a client, not a component

The Streamlit app talks to the FastAPI service over HTTP and never imports the
store layer. That keeps one owner for filtering, search and pagination, and means
the dashboard can be restarted, scaled, or pointed at a remote API without
touching the dataset.

Because Streamlit reruns the entire script on every widget interaction, the two
things that would otherwise turn a dashboard into a load generator are handled
explicitly: a `@st.cache_resource` HTTP client (connection pooling) and
`@st.cache_data(ttl=...)` on API calls.

Each page is a `render(client)` function rather than top-level script code. That
one structural choice is what makes the UI testable headlessly with
`streamlit.testing.v1.AppTest`, with a stub client and no server.

**A bug this arrangement cannot catch on its own:** four `st.Page(lambda: ...)`
definitions inferred the same `<lambda>` URL path, which `st.navigation` rejects
at runtime — the individual pages rendered perfectly in isolation. `build_pages()`
was extracted so a test can construct the real navigation and assert it builds.

---

## 11. Testing strategy

| Level | Technique | What it protects |
|---|---|---|
| Pure functions | Direct unit tests | salary/date parsing, PII scrubbing, URL canonicalisation |
| Policy | Injected clock and transport | rate limiting, backoff, breaker transitions, robots verdicts |
| Integration | Fixture transport + real fetcher | the whole pipeline, offline |
| Contract | Recorded HTML fixtures | selector regressions, DOM drift |
| Data | Temp SQLite via real repositories | upsert semantics, pagination, staleness |
| Dialect | Compile for PostgreSQL, assert the SQL | the `ON CONFLICT` guard, tsvector config, `date_trunc` |
| Schema | Migration vs. metadata comparison | drift between models and migrations |
| Live server | `pytest -m integration` on PostgreSQL | JSONB, real FTS, concurrency, rollback |
| UI | `AppTest` | every dashboard page and the navigation shell |

The fixture adapter is the keystone. Because it serves recorded pages through an
`httpx` transport into the **real** `HttpFetcher`, the offline tests exercise
robots handling, token buckets, retry classification, archiving and metrics
exactly as production does. Only the socket is fake. That is a far stronger
guarantee than mocking the crawler and hoping the real one behaves.

---

## 12. Deliberate omissions

- **No JavaScript rendering.** The extension point is a Playwright-backed
  `Fetcher` transport; nothing else in the design would need to change.
- **No proxy rotation.** It optimises throughput at the cost of looking evasive,
  which is the wrong trade for a project whose selling point is politeness.
- **No per-host work queues in the frontier.** Correct for bounded job-board
  crawls; the first thing to add for a large multi-domain crawl.
- **No authentication.** Nothing here should require logging in.
