"""Listings page: filter, search, paginate, inspect a single row.

Filters live in the sidebar, query results are cached, and row selection reuses
``st.dataframe``'s native selection so no full page navigation is needed.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from jobscout.dashboard.client import ApiUnavailable, JobScoutClient

PAGE_SIZES = [25, 50, 100]


def render(client: JobScoutClient) -> None:
    """Draw the listings page."""
    st.title("Listings")
    filters = _sidebar(client)

    try:
        page = client.listings(**filters)
    except ApiUnavailable as exc:
        st.error(str(exc))
        return

    if not page.listings:
        st.info("No listings match these filters.")
        return

    st.caption(f"{page.count} listing(s) on this page")
    frame = _to_frame(page.listings)

    selection = st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Posted": st.column_config.DateColumn("Posted", format="YYYY-MM-DD"),
            "Salary": st.column_config.TextColumn("Salary", width="small"),
        },
    )

    rows = _selected_rows(selection, page.listings)
    if rows:
        _detail(client, rows[0])


def _sidebar(client: JobScoutClient) -> dict[str, Any]:
    """Build the filter payload from sidebar widgets."""
    with st.sidebar:
        st.header("Filters")
        keyword = st.text_input("Search", placeholder="e.g. python, kubernetes")
        companies = st.multiselect("Company", _facets(client, "company"), max_selections=10)
        location = st.text_input("Location contains")
        remote_only = st.checkbox("Remote only", value=False)
        employment_types = st.multiselect("Employment type", _facets(client, "employment_type"))
        sources = st.multiselect("Source", _facets(client, "source"))
        min_salary = st.number_input(
            "Minimum salary", min_value=0, max_value=500_000, step=10_000, value=0
        )
        include_inactive = st.checkbox("Include no-longer-listed", value=False)
        page_size = st.selectbox("Page size", PAGE_SIZES, index=0)
        sort = st.selectbox("Sort by", ["posted_at", "first_seen_at", "last_seen_at", "company"])
        descending = st.checkbox("Descending", value=True)

    return {
        "q": keyword or None,
        "companies": companies,
        "location": location or None,
        "remote": True if remote_only else None,
        "employment_type": employment_types[0] if employment_types else None,
        "source": sources[0] if sources else None,
        "min_salary": float(min_salary) if min_salary else None,
        "include_inactive": include_inactive,
        "sort": sort,
        "desc": descending,
        "limit": int(page_size),
    }


@st.cache_data(ttl=300, show_spinner=False)
def _facets_cached(base_url: str, field_name: str) -> tuple[str, ...]:
    with JobScoutClient(base_url) as client:
        return tuple(client.facets(field_name))


def _facets(client: JobScoutClient, field_name: str) -> list[str]:
    """Facet values, cached per API base URL and field."""
    try:
        return list(_facets_cached(client.base_url, field_name))
    except ApiUnavailable:
        return []


def _to_frame(listings: list[Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ID": item.id,
                "Title": item.title,
                "Company": item.company,
                "Location": item.location or "—",
                "Remote": item.remote,
                "Type": item.employment_type or "—",
                "Salary": item.salary_display,
                "Posted": item.posted_at.date() if item.posted_at else None,
            }
            for item in listings
        ]
    )


def _selected_rows(selection: Any, listings: list[Any]) -> list[Any]:
    """Map a dataframe selection back onto listing objects."""
    rows = getattr(selection, "selection", None)
    indices = getattr(rows, "rows", None) if rows is not None else None
    if not indices:
        return []
    return [listings[index] for index in indices if 0 <= index < len(listings)]


def _detail(client: JobScoutClient, listing: Any) -> None:
    st.divider()
    st.subheader(listing.title)
    st.caption(f"{listing.company} · {listing.location or 'location not given'}")

    try:
        detail = client.listing(listing.id)
    except ApiUnavailable as exc:
        st.error(str(exc))
        return

    meta = st.columns(4)
    meta[0].metric("Salary", listing.salary_display)
    meta[1].metric("Type", listing.employment_type or "—")
    meta[2].metric("Remote", "Yes" if listing.remote else "No")
    meta[3].metric("First seen", listing.first_seen_at.date().isoformat())

    if listing.tags:
        st.write(" ".join(f"`{tag}`" for tag in listing.tags))

    if detail.get("description"):
        st.markdown(detail["description"])
    else:
        st.caption("No description captured for this listing.")

    st.link_button("Open original posting", detail.get("url", "#"))
