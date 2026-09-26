"""View package: one module per Streamlit page.

Each module exposes ``render(client)`` rather than touching Streamlit globals at
import time, which is what allows the pages to be unit tested with
``streamlit.testing.v1.AppTest``.
"""

from __future__ import annotations

__all__ = ["companies", "listings", "overview", "runs"]
