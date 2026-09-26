"""Frontier behaviour: dedupe, depth limiting, ordering and budgets."""

from __future__ import annotations

import pytest

from jobscout.models import Follow
from jobscout.pipeline.frontier import Frontier, canonical_url, same_site


def follow(url: str, **meta: object) -> Follow:
    return Follow(url=url, kind="html", meta=dict(meta))


def test_canonical_url_drops_fragment_and_sorts_query() -> None:
    assert (
        canonical_url("HTTPS://Example.COM/jobs?b=2&a=1#top") == "https://example.com/jobs?a=1&b=2"
    )
    assert canonical_url("https://example.com/jobs") == canonical_url("https://example.com/jobs")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("https://jobs.test/a", "https://jobs.test/b", True),
        ("https://www.jobs.test/a", "https://jobs.test/b", True),
        ("https://jobs.test/a", "https://other.test/b", False),
        ("", "https://jobs.test", False),
    ],
)
def test_same_site(a: str, b: str, expected: bool) -> None:
    assert same_site(a, b) is expected


def test_pops_shallowest_first() -> None:
    frontier = Frontier(max_depth=3)
    frontier.push(follow("https://x.test/deep"), depth=2)
    frontier.push(follow("https://x.test/shallow"), depth=0)

    first, depth = frontier.pop()  # type: ignore[misc]
    assert first.url == "https://x.test/shallow"
    assert depth == 0


def test_deduplicates_canonical_urls() -> None:
    frontier = Frontier(max_depth=2)

    assert frontier.push(follow("https://x.test/jobs?a=1&b=2"), depth=0) is True
    assert frontier.push(follow("https://x.test/jobs?b=2&a=1#frag"), depth=0) is False
    assert frontier.stats.duplicates == 1
    assert len(frontier) == 1


def test_depth_limit_drops_deep_follows() -> None:
    frontier = Frontier(max_depth=1)

    assert frontier.push(follow("https://x.test/ok"), depth=1) is True
    assert frontier.push(follow("https://x.test/too-deep"), depth=2) is False
    assert frontier.stats.too_deep == 1
    assert frontier.stats.max_depth_reached == 1


def test_max_items_budget() -> None:
    frontier = Frontier(max_depth=5, max_items=3)

    for index in range(10):
        frontier.push(follow(f"https://x.test/{index}"), depth=0)

    assert len(frontier) == 3
    assert frontier.stats.over_budget == 7


def test_seeds_are_pre_deduplicated() -> None:
    frontier = Frontier(max_depth=1, seeds=["https://x.test/start"])

    assert frontier.push(follow("https://x.test/start"), depth=0) is False


def test_empty_frontier_is_falsy_and_pops_none() -> None:
    frontier = Frontier()
    assert not frontier
    assert frontier.pop() is None


def test_iteration_drains_the_queue() -> None:
    frontier = Frontier(max_depth=1)
    frontier.push_all([follow(f"https://x.test/{i}") for i in range(3)], depth=0)

    drained = list(frontier)

    assert len(drained) == 3
    assert frontier.stats.popped == 3
