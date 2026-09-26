"""Crawl pipeline: frontier scheduling and run orchestration."""

from __future__ import annotations

from jobscout.pipeline.crawler import Crawler, CrawlResult
from jobscout.pipeline.frontier import Frontier, FrontierStats, canonical_url, same_site
from jobscout.pipeline.runner import ReParseReport, reparse_snapshots, run_crawl

__all__ = [
    "CrawlResult",
    "Crawler",
    "Frontier",
    "FrontierStats",
    "ReParseReport",
    "canonical_url",
    "reparse_snapshots",
    "run_crawl",
    "same_site",
]
