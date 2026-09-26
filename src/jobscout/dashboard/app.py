"""Streamlit dashboard entrypoint.

Run it with::

    streamlit run src/jobscout/dashboard/app.py

The dashboard is a separate service from the API on purpose: it holds no data of
its own, so it can be restarted, scaled or pointed at a remote API without
touching the dataset.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from jobscout.dashboard.client import ApiUnavailable, JobScoutClient
from jobscout.dashboard.views import companies, listings, overview, runs

PAGE_TITLE = "JobScout"
PAGE_ICON = "🛰️"


@st.cache_resource(show_spinner=False)
def get_client() -> JobScoutClient:
    """One pooled HTTP client per session; Streamlit reruns are cheap."""
    return JobScoutClient()


def build_pages(client: JobScoutClient) -> list[Any]:
    """Page definitions.

    Kept separate from :func:`main` so the navigation itself is testable, and so
    every page declares an explicit ``url_path``: Streamlit infers one from the
    callable, and four lambdas would all infer the same ``<lambda>`` pathname,
    which ``st.navigation`` rejects at runtime.
    """
    return [
        st.Page(
            lambda: overview.render(client),
            title="Overview",
            icon=":material/dashboard:",
            url_path="",
            default=True,
        ),
        st.Page(
            lambda: listings.render(client),
            title="Listings",
            icon=":material/work:",
            url_path="listings",
        ),
        st.Page(
            lambda: companies.render(client),
            title="Companies",
            icon=":material/apartment:",
            url_path="companies",
        ),
        st.Page(
            lambda: runs.render(client),
            title="Crawl runs",
            icon=":material/history:",
            url_path="runs",
        ),
    ]


def _sidebar(client: JobScoutClient) -> None:
    status = client.status()
    with st.sidebar:
        st.caption(f"API: `{client.base_url}`")
        if status.reachable:
            st.success("API reachable")
            if status.latest_run:
                st.caption(f"Last successful crawl: {status.latest_run:%Y-%m-%d %H:%M} UTC")
            else:
                st.caption("No successful crawl recorded yet.")
        else:
            st.error("API unreachable")
            st.caption(status.detail)


def main() -> None:
    """Build navigation and render the selected page."""
    st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="wide")

    client = get_client()
    _sidebar(client)

    navigation = st.navigation(build_pages(client))
    try:
        navigation.run()
    except ApiUnavailable as exc:
        st.error(str(exc))
        st.stop()


if __name__ == "__main__":
    main()
