"""Crawl runs page: did the crawler work, and what did it cost."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from jobscout.dashboard.client import ApiUnavailable, JobScoutClient

STATUS_COLOURS = {"succeeded": "#54A24B", "running": "#4C78A8", "failed": "#E45756"}


def render(client: JobScoutClient) -> None:
    """Draw the crawl-runs page."""
    st.title("Crawl runs")
    st.caption("Every run records what it fetched, what changed and what failed.")

    try:
        runs = client.runs(limit=50)
    except ApiUnavailable as exc:
        st.error(str(exc))
        return

    if not runs:
        st.info("No runs recorded yet.")
        return

    frame = pd.DataFrame(runs)
    display = frame.rename(
        columns={
            "id": "Run",
            "adapter": "Adapter",
            "status": "Status",
            "started_at": "Started",
            "duration_seconds": "Seconds",
            "pages_fetched": "Pages",
            "items_seen": "Seen",
            "items_new": "New",
            "items_changed": "Changed",
            "items_unchanged": "Unchanged",
            "failures": "Failures",
        }
    )[
        [
            "Run",
            "Adapter",
            "Status",
            "Started",
            "Seconds",
            "Pages",
            "Seen",
            "New",
            "Changed",
            "Unchanged",
            "Failures",
        ]
    ]
    st.dataframe(display, width="stretch", hide_index=True)

    trend = (
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=alt.X("started_at:T", title=None, axis=alt.Axis(format="%b %d %H:%M")),
            y=alt.Y("items_new:Q", title="new listings"),
            color=alt.Color(
                "status:N",
                scale=alt.Scale(domain=list(STATUS_COLOURS), range=list(STATUS_COLOURS.values())),
                legend=alt.Legend(title="status"),
            ),
            tooltip=[
                "adapter:N",
                "status:N",
                "items_new:Q",
                "items_changed:Q",
                "duration_seconds:Q",
            ],
        )
        .properties(height=260)
    )
    st.altair_chart(trend, width="stretch")

    last = runs[0]
    st.subheader(f"Run {last['id']} detail")
    columns = st.columns(4)
    columns[0].metric("Pages", last.get("pages_fetched", 0))
    columns[1].metric("New", last.get("items_new", 0))
    columns[2].metric("Changed", last.get("items_changed", 0))
    columns[3].metric("Failures", last.get("failures", 0))

    detail = last.get("detail") or {}
    if detail:
        st.json(detail)
