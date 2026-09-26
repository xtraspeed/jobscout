"""JobScout: an async, production-shaped public job-listing scraper.

The package is layered so each concern can be tested in isolation:

``fetch``      pooled HTTP, per-domain politeness, retries, circuit breaker
``adapters``   site-specific discovery + parsing behind one protocol
``parse``      selectolax helpers, text normalisation, PII scrubbing
``pipeline``   frontier scheduling and crawl orchestration
``store``      async SQLAlchemy models and repositories
``api``        FastAPI query service
``dashboard``  Streamlit analytics UI (talks to the API over HTTP)
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
