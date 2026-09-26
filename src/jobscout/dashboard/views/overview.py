"""Overview page: headline numbers and trends.

Rendered with Altair (bundled with Streamlit) so charts are declarative and
there is no extra plotting dependency to install.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from jobscout.dashboard.client import JobScoutClient


def render(client: JobScoutClient) -> None:
    """Draw the overview page."""
    st.title("JobScout")
    st.caption("Public job listings collected by the JobScout crawler.")

    days = st.slider("Window (days)", min_value=7, max_value=180, value=30, step=1)

    stats = client.stats(days=days)
    _metric_row(stats)

    daily = pd.DataFrame(stats.get("daily") or [])
    if daily.empty:
        st.info("No listings yet. Run `jobscout crawl --target hnhiring` to collect some.")
        return

    daily["day"] = pd.to_datetime(daily["day"])
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Listings posted per day")
        trend = (
            alt.Chart(daily)
            .mark_area(color="#4C78A8", opacity=0.25)
            .encode(
                x=alt.X("day:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0)),
                y=alt.Y("count:Q", title="listings", stack=None),
                tooltip=[
                    alt.Tooltip("day:T", title="Day"),
                    alt.Tooltip("count:Q", title="Listings"),
                ],
            )
            .properties(height=260)
            .interactive()
        )
        st.altair_chart(trend, width="stretch")

    with right:
        st.subheader("Sources")
        sources = stats.get("by_source") or {}
        if sources:
            frame = pd.DataFrame(
                [{"source": key, "count": value} for key, value in sources.items()]
            )
            bars = (
                alt.Chart(frame)
                .mark_bar(color="#F58518")
                .encode(
                    y=alt.Y("source:N", sort="-x", title=None),
                    x=alt.X("count:Q", title="listings"),
                    tooltip=["source:N", "count:Q"],
                )
                .properties(height=260)
            )
            st.altair_chart(bars, width="stretch")
        else:
            st.caption("No source breakdown available.")


def _metric_row(stats: dict[str, Any]) -> None:
    total = int(stats.get("total", 0))
    active = int(stats.get("active", 0))
    remote = int(stats.get("remote", 0))
    with_salary = int(stats.get("with_salary", 0))
    new_recent = int(stats.get("new_recent", 0))
    window = int(stats.get("window_days", 30))

    remote_pct = f"{(remote / active * 100):.0f}%" if active else "—"
    salary_pct = f"{(with_salary / active * 100):.0f}%" if active else "—"

    columns = st.columns(5)
    columns[0].metric("Listings", f"{total:,}")
    columns[1].metric("Active", f"{active:,}")
    columns[2].metric("Remote", remote_pct)
    columns[3].metric("With salary", salary_pct)
    columns[4].metric(f"New (last {window}d)", f"{new_recent:,}")
