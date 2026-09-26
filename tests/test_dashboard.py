"""Dashboard tests.

Streamlit ships a headless test harness (``streamlit.testing.v1.AppTest``), so
the UI is genuinely covered in CI rather than eyeballed. Each page is exposed as
``render(client)``, which lets the harness call it with a stub client and no HTTP
server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from jobscout.dashboard.client import JobScoutClient, Listing
from jobscout.dashboard.views import companies, listings, overview, runs

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def make_listing(index: int = 1, **overrides: Any) -> Listing:
    defaults: dict[str, Any] = {
        "id": index,
        "title": "Senior Backend Engineer",
        "company": "Northwind Analytics",
        "location": "Berlin, Germany",
        "remote": False,
        "employment_type": "full-time",
        "salary_min": 85_000.0,
        "salary_max": 105_000.0,
        "salary_currency": "EUR",
        "posted_at": NOW,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "tags": ["Python"],
        "source": "demo_board",
    }
    defaults.update(overrides)
    return Listing(**defaults)


class FakeClient:
    """Stub implementing the same surface as :class:`JobScoutClient`."""

    base_url = "http://api.test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    def _check(self, name: str) -> None:
        from jobscout.dashboard.client import ApiUnavailable

        self.calls.append(name)
        if self.fail:
            raise ApiUnavailable("api down")

    def status(self) -> Any:
        from jobscout.dashboard.client import ApiStatus

        # Mirrors the real client: an unreachable API is reported as a status,
        # never raised.
        self.calls.append("status")
        if self.fail:
            return ApiStatus(reachable=False, detail="cannot reach http://api.test")
        return ApiStatus(reachable=True, detail="ok", latest_run=NOW)

    def stats(self, *, days: int = 30) -> dict[str, Any]:
        self._check("stats")
        return {
            "total": 1200,
            "active": 1100,
            "remote": 640,
            "with_salary": 320,
            "new_recent": 85,
            "window_days": days,
            "by_source": {"demo_board": 900, "hnhiring": 300},
            "daily": [
                {"day": "2026-02-27", "count": 10},
                {"day": "2026-02-28", "count": 25},
                {"day": "2026-03-01", "count": 50},
            ],
        }

    def listings(self, **kwargs: Any) -> Any:
        from jobscout.dashboard.client import ListingPage

        self._check("listings")
        if kwargs.get("q") == "nothing-matches":
            return ListingPage(listings=[], next_cursor=None, count=0)
        return ListingPage(
            listings=[make_listing(1), make_listing(2, company="Contoso Labs", remote=True)],
            next_cursor="next-page-token",
            count=2,
        )

    def listing(self, item_id: int) -> dict[str, Any]:
        self._check("listing")
        return {
            "id": item_id,
            "title": "Senior Backend Engineer",
            "company": "Northwind Analytics",
            "description": "Own the ingestion pipeline.",
            "url": "https://demo-board.example/jobs/1001",
            "tags": ["Python"],
        }

    def companies(
        self, *, limit: int = 100, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        self._check("companies")
        return [
            {
                "company": "Northwind Analytics",
                "listings": 42,
                "remote_listings": 30,
                "latest_posted": NOW.isoformat(),
                "max_salary": 180_000.0,
            },
            {
                "company": "Contoso Labs",
                "listings": 17,
                "remote_listings": 17,
                "latest_posted": NOW.isoformat(),
                "max_salary": 220_000.0,
            },
        ]

    def facets(self, field_name: str, *, limit: int = 50) -> list[str]:
        self._check("facets")
        return {
            "company": ["Northwind Analytics", "Contoso Labs"],
            "employment_type": ["full-time", "contract"],
            "source": ["demo_board"],
        }.get(field_name, [])

    def runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._check("runs")
        return [
            {
                "id": 2,
                "adapter": "demo_board",
                "status": "succeeded",
                "started_at": NOW.isoformat(),
                "finished_at": NOW.isoformat(),
                "duration_seconds": 12.5,
                "pages_fetched": 40,
                "items_seen": 38,
                "items_new": 4,
                "items_changed": 1,
                "items_unchanged": 33,
                "failures": 2,
                "detail": {"breakers": {}, "duplicates_skipped": 1},
            },
            {
                "id": 1,
                "adapter": "hnhiring",
                "status": "failed",
                "started_at": NOW.isoformat(),
                "duration_seconds": 3.0,
                "pages_fetched": 5,
                "items_seen": 0,
                "items_new": 0,
                "items_changed": 0,
                "items_unchanged": 0,
                "failures": 5,
                "detail": {},
            },
        ]


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def run_view(view: Any, client: FakeClient) -> AppTest:
    """Render one page headlessly with a stub client.

    ``AppTest.from_function`` execs a function's source in a bare namespace, so
    its annotations cannot resolve. A tiny generated script plus session-state
    injection keeps the page modules importable as normal Python.
    """
    listings._facets_cached.clear()
    module = view.__name__.rsplit(".", 1)[-1]
    script = (
        "import streamlit as st\n"
        f"from jobscout.dashboard.views import {module} as view\n"
        "view.render(st.session_state['client'])\n"
    )
    app = AppTest.from_string(script, default_timeout=30)
    app.session_state["client"] = client
    app.run()
    return app


# ----------------------------------------------------------------- overview --


def test_overview_renders_metrics(client: FakeClient) -> None:
    app = run_view(overview, client)

    assert not app.exception
    labels = [metric.label for metric in app.metric]
    assert "Listings" in labels
    assert "Active" in labels
    assert "Remote" in labels
    assert "With salary" in labels
    values = {metric.label: metric.value for metric in app.metric}
    assert values["Listings"] == "1,200"
    assert values["Remote"] == "58%", "remote share is shown as a percentage"


def test_overview_renders_charts(client: FakeClient) -> None:
    app = run_view(overview, client)

    assert not app.exception
    assert len(app.get("vega_lite_chart")) == 2, "trend and source charts"


def test_overview_handles_an_empty_dataset(client: FakeClient) -> None:
    def empty_stats(*, days: int = 30) -> dict[str, Any]:
        return {
            "total": 0,
            "active": 0,
            "remote": 0,
            "with_salary": 0,
            "new_recent": 0,
            "window_days": days,
            "by_source": {},
            "daily": [],
        }

    client.stats = empty_stats  # type: ignore[method-assign]
    app = run_view(overview, client)

    assert not app.exception
    assert any("No listings yet" in info.value for info in app.info)


# ----------------------------------------------------------------- listings --


def test_listings_renders_a_table(client: FakeClient) -> None:
    app = run_view(listings, client)

    assert not app.exception
    assert len(app.dataframe) == 1
    assert "2 listing(s) on this page" in app.caption[0].value


def test_listings_renders_sidebar_filters(client: FakeClient) -> None:
    app = run_view(listings, client)

    assert not app.exception
    labels = {label.label for label in app.sidebar.text_input} | {
        label.label for label in app.sidebar.checkbox
    }
    assert {"Search", "Location contains", "Remote only"} <= labels


def test_listings_shows_empty_state(client: FakeClient) -> None:
    app = run_view(listings, client)

    # Type a query that matches nothing and let Streamlit rerun the script.
    app.sidebar.text_input[0].set_value("nothing-matches").run()

    assert not app.exception
    assert any("No listings match" in info.value for info in app.info)


def test_listings_filters_are_passed_to_the_api(client: FakeClient) -> None:
    app = run_view(listings, client)
    app.sidebar.checkbox[0].set_value(True).run()  # "Remote only"
    app.run()

    assert not app.exception
    assert "Remote only" in [label.label for label in app.sidebar.checkbox]


def test_listings_renders_detail_for_a_selected_row(client: FakeClient) -> None:
    """Row selection -> detail view.

    Streamlit's test harness cannot drive ``st.dataframe`` row selection, so the
    mapping that the ``on_select="rerun"`` callback relies on is covered directly.
    """
    from types import SimpleNamespace

    page_listings = [make_listing(1), make_listing(2, company="Contoso Labs")]
    selection = SimpleNamespace(selection=SimpleNamespace(rows=[1]))

    rows = listings._selected_rows(selection, page_listings)

    assert [row.company for row in rows] == ["Contoso Labs"]


def test_selected_rows_ignores_empty_and_out_of_range_selections() -> None:
    from types import SimpleNamespace

    page_listings = [make_listing(1)]
    empty = SimpleNamespace(selection=SimpleNamespace(rows=[]))
    out_of_range = SimpleNamespace(selection=SimpleNamespace(rows=[99]))
    no_selection = SimpleNamespace(selection=None)

    assert listings._selected_rows(empty, page_listings) == []
    assert listings._selected_rows(out_of_range, page_listings) == []
    assert listings._selected_rows(no_selection, page_listings) == []


def test_listing_frame_has_stable_columns() -> None:
    frame = listings._to_frame([make_listing(1), make_listing(2, location=None, remote=True)])

    assert list(frame.columns) == [
        "ID",
        "Title",
        "Company",
        "Location",
        "Remote",
        "Type",
        "Salary",
        "Posted",
    ]
    assert frame["Location"].iloc[1] == "—"


def test_listings_surfaces_api_errors(client: FakeClient) -> None:
    client.fail = True
    app = run_view(listings, client)

    assert not app.exception
    assert any("api down" in error.value for error in app.error)


# --------------------------------------------------------------- companies --


def test_companies_renders_rollup_and_chart(client: FakeClient) -> None:
    app = run_view(companies, client)

    assert not app.exception
    assert len(app.dataframe) == 1
    assert len(app.get("vega_lite_chart")) == 1
    assert "Most advertised" in app.subheader[0].value


def test_companies_handles_no_data(client: FakeClient) -> None:
    client.companies = lambda **_: []  # type: ignore[method-assign]
    app = run_view(companies, client)

    assert not app.exception
    assert any("No company data" in info.value for info in app.info)


# -------------------------------------------------------------------- runs --


def test_runs_renders_history_and_detail(client: FakeClient) -> None:
    app = run_view(runs, client)

    assert not app.exception
    assert len(app.dataframe) == 1
    assert "Run 2 detail" in app.subheader[-1].value
    labels = {metric.label for metric in app.metric}
    assert {"Pages", "New", "Changed", "Failures"} <= labels


def test_runs_handles_no_runs(client: FakeClient) -> None:
    client.runs = lambda **_: []  # type: ignore[method-assign]
    app = run_view(runs, client)

    assert not app.exception
    assert any("No runs recorded" in info.value for info in app.info)


# ------------------------------------------------------------------ client --


def test_listing_salary_display_formats_ranges() -> None:
    assert make_listing().salary_display == "EUR 85k – 105k"
    assert make_listing(salary_min=90_000.0, salary_max=90_000.0).salary_display == "EUR 90k"
    assert make_listing(salary_min=None, salary_max=None).salary_display == "—"


def test_listing_posted_display() -> None:
    assert make_listing().posted_display == "2026-03-01"
    assert make_listing(posted_at=None).posted_display == "—"


def test_client_reports_unreachable_api() -> None:
    """A dead API must surface as a status object, not an exception."""
    client = JobScoutClient("http://127.0.0.1:9", timeout=0.2)
    try:
        status = client.status()
    finally:
        client.close()

    assert status.reachable is False
    assert "cannot reach" in status.detail


# ---------------------------------------------------------------- the shell --


NAV_SCRIPT = """
import streamlit as st
from jobscout.dashboard import app

client = st.session_state['client']
app._sidebar(client)
st.navigation(app.build_pages(client)).run()
"""


def run_nav(client: FakeClient) -> AppTest:
    app_test = AppTest.from_string(NAV_SCRIPT, default_timeout=30)
    app_test.session_state["client"] = client
    app_test.run()
    return app_test


def test_navigation_builds_without_error(client: FakeClient) -> None:
    """Regression: four lambda-backed pages once collided on one URL path.

    ``st.navigation`` derives a page's path from its callable, so every page has
    to declare an explicit ``url_path``. Testing the pages individually cannot
    catch this; only building the real navigation can.
    """
    app_test = run_nav(client)

    assert not app_test.exception, [str(e.value) for e in app_test.exception]
    assert len(app_test._registered_pages) == 4


def test_default_page_renders_overview(client: FakeClient) -> None:
    app_test = run_nav(client)

    assert not app_test.exception
    assert {metric.label for metric in app_test.metric} >= {"Listings", "Active"}
    assert "Listings posted per day" in [s.value for s in app_test.subheader]


def test_sidebar_reports_api_health(client: FakeClient) -> None:
    app_test = run_nav(client)

    assert [s.value for s in app_test.sidebar.success] == ["API reachable"]
    assert not app_test.sidebar.error
    assert any("Last successful crawl" in c.value for c in app_test.sidebar.caption)


def test_sidebar_reports_an_unreachable_api(client: FakeClient) -> None:
    client.fail = True
    app_test = AppTest.from_string(
        "import streamlit as st\n"
        "from jobscout.dashboard import app\n"
        "client = st.session_state['client']\n"
        "app._sidebar(client)\n",
        default_timeout=30,
    )
    app_test.session_state["client"] = client
    app_test.run()

    assert [e.value for e in app_test.sidebar.error] == ["API unreachable"]
    assert any("cannot reach" in c.value for c in app_test.sidebar.caption)


def test_page_url_paths_are_unique() -> None:
    """Declared explicitly, so this cannot silently regress."""
    import inspect

    from jobscout.dashboard import app as dashboard_app

    source = inspect.getsource(dashboard_app.build_pages)
    paths = [
        line.split('"')[1] for line in source.splitlines() if line.strip().startswith("url_path=")
    ]

    assert len(paths) == 4
    assert len(set(paths)) == 4
    assert "" in paths, "the default page owns the root path"
