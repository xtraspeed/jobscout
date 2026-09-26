"""Hacker News "Ask HN: Who is hiring?" adapter.

Three-stage crawl over documented public APIs:

1. ``search``  — the public Algolia HN search API returns the monthly hiring
   threads; each becomes a ``Follow``.
2. ``thread``  — the Firebase item API returns a thread and its top-level
   comment ids; each comment becomes a ``Follow``.
3. ``comment`` — a comment body is free-form company text wrapped in HTML.
   Comments that look like postings become items; their replies become more
   ``Follow`` objects, so the crawl naturally descends the thread.

Stage is carried in ``Follow.meta``, so ``parse`` never has to guess from a URL.
Only the first two levels are queued by default, which is exactly the
"top-level comments" set companies use to advertise roles.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlencode

from jobscout.adapters.base import BaseAdapter
from jobscout.models import Emitted, FetchedPage, Follow
from jobscout.observability import get_logger
from jobscout.parse.hn import (
    HN_ITEM_BASE,
    HN_SEARCH_API,
    is_hiring_thread,
    parse_comment,
    posting_to_item,
    thread_posted_at,
)

_log = get_logger(__name__)

STAGE_SEARCH = "search"
STAGE_THREAD = "thread"
STAGE_COMMENT = "comment"

DEFAULT_QUERY: Final = "Ask HN: Who is hiring"


class HnHiringAdapter(BaseAdapter):
    """Turns HN hiring threads into structured postings."""

    name = "hnhiring"

    def __init__(
        self,
        *,
        threads: int = 2,
        hits_per_page: int = 30,
        query: str = DEFAULT_QUERY,
        max_comments_per_thread: int = 400,
        descend_replies: bool = False,
    ) -> None:
        self.threads = threads
        self.hits_per_page = hits_per_page
        self.query = query
        self.max_comments_per_thread = max_comments_per_thread
        self.descend_replies = descend_replies

    async def start(self) -> Sequence[Follow]:
        url = f"{HN_SEARCH_API}?{urlencode({'query': self.query, 'tags': 'story', 'hitsPerPage': self.hits_per_page})}"
        _log.info("hnhiring.start", query=self.query, threads=self.threads)
        return [Follow(url=url, kind="json", meta={"stage": STAGE_SEARCH})]

    def parse(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        stage = str(follow.meta.get("stage", ""))
        if stage == STAGE_SEARCH:
            return self._parse_search(page)
        if stage == STAGE_THREAD:
            return self._parse_thread(page, follow)
        if stage == STAGE_COMMENT:
            return self._parse_comment(page, follow)
        _log.debug("hnhiring.unknown_stage", stage=stage, url=page.url)
        return []

    # -- stage 1: which threads? -------------------------------------------

    def _parse_search(self, page: FetchedPage) -> Sequence[Follow]:
        payload = _as_dict(page)
        if payload is None:
            _log.warning("hnhiring.bad_json", url=page.url)
            return []

        hits = [hit for hit in payload.get("hits", []) if is_hiring_thread(hit.get("title"))]
        follows: list[Follow] = []
        for hit in hits[: self.threads]:
            object_id = hit.get("objectID")
            if object_id is None:
                continue
            posted = thread_posted_at(hit.get("created_at_i") or hit.get("created_at"))
            follows.append(
                Follow(
                    url=f"{HN_ITEM_BASE}/{object_id}.json",
                    kind="json",
                    meta={
                        "stage": STAGE_THREAD,
                        "thread_id": int(object_id),
                        "title": hit.get("title"),
                        "posted_at": _iso(posted),
                    },
                )
            )
        _log.info("hnhiring.threads_found", count=len(follows), hits=len(payload.get("hits", [])))
        return follows

    # -- stage 2: which comments? ------------------------------------------

    def _parse_thread(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        story = _as_dict(page)
        if story is None:
            _log.warning("hnhiring.bad_json", url=page.url)
            return []

        thread_id = int(follow.meta.get("thread_id") or story.get("id") or 0)
        posted = thread_posted_at(story.get("time")) or _parse_iso(follow.meta.get("posted_at"))
        comment_ids = list(story.get("kids") or [])[: self.max_comments_per_thread]

        follows = [
            Follow(
                url=f"{HN_ITEM_BASE}/{comment_id}.json",
                kind="json",
                meta={
                    "stage": STAGE_COMMENT,
                    "thread_id": thread_id,
                    "depth": 1,
                    "posted_at": _iso(posted),
                },
            )
            for comment_id in comment_ids
        ]
        _log.info("hnhiring.thread_parsed", thread_id=thread_id, comments=len(follows))
        return follows

    # -- stage 3: is this comment a posting? --------------------------------

    def _parse_comment(self, page: FetchedPage, follow: Follow) -> Sequence[Follow | Emitted]:
        comment = _as_dict(page)
        if comment is None or comment.get("deleted") or comment.get("dead"):
            return []

        depth = int(follow.meta.get("depth", 1))
        thread_id = int(follow.meta.get("thread_id") or comment.get("parent_id") or 0)

        replies: list[Follow] = []
        if self.descend_replies and depth < 2:
            replies = [
                Follow(
                    url=f"{HN_ITEM_BASE}/{child_id}.json",
                    kind="json",
                    meta={"stage": STAGE_COMMENT, "thread_id": thread_id, "depth": depth + 1},
                )
                for child_id in (comment.get("kids") or [])[: self.max_comments_per_thread]
            ]

        posting = parse_comment(comment.get("text"))
        if posting is None:
            return replies

        item = posting_to_item(
            posting,
            comment_id=int(comment.get("id", 0)),
            thread_id=thread_id,
        )
        if follow.meta.get("posted_at"):
            item = item.model_copy(update={"posted_at": _parse_iso(follow.meta["posted_at"])})
        return [Emitted(item=item, source_url=page.url), *replies]


def _as_dict(page: FetchedPage) -> dict[str, Any] | None:
    try:
        payload = page.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
