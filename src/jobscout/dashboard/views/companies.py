"""Companies page: who is hiring, and how much they advertise."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from jobscout.dashboard.client import ApiUnavailable, JobScoutClient


def render(client: JobScoutClient) -> None:
    """Draw the companies page."""
    st.title("Companies")
    st.caption("Aggregated from active listings only.")

    limit = st.slider("Companies to show", min_value=10, max_value=500, value=50, step=10)
    try:
        rows = client.companies(limit=limit)
    except ApiUnavailable as exc:
        st.error(str(exc))
        return

    if not rows:
        st.info("No company data yet.")
        return

    frame = pd.DataFrame(rows)
    frame = frame.rename(
        columns={
            "company": "Company",
            "listings": "Listings",
            "remote_listings": "Remote",
            "latest_posted": "Latest posted",
            "max_salary": "Max salary",
        }
    )
    if "Latest posted" in frame:
        frame["Latest posted"] = pd.to_datetime(frame["Latest posted"], errors="coerce").dt.date
    if "Max salary" in frame:
        frame["Max salary"] = frame["Max salary"].map(lambda v: f"{v:,.0f}" if pd.notna(v) else "—")

    st.dataframe(frame, width="stretch", hide_index=True)

    top = frame.head(15)
    st.subheader("Most advertised")
    chart = (
        alt.Chart(top)
        .mark_bar(color="#54A24B")
        .encode(
            y=alt.Y("Company:N", sort="-x", title=None),
            x=alt.X("Listings:Q", title="active listings"),
            tooltip=["Company:N", "Listings:Q", "Remote:Q"],
        )
        .properties(height=max(240, 22 * len(top)))
    )
    st.altair_chart(chart, width="stretch")
