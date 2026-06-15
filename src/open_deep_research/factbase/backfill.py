"""Backfill run_source by independently fetching cited URLs' raw text.

Decouples discovery (whatever search the run used) from evidence capture, so facts
can be span-verified against independently-fetched source text on ANY search backend.
"""
from __future__ import annotations


async def backfill_run_sources(store, thread_id: str, urls: list[str], fetcher,
                               *, max_urls: int = 20) -> dict:
    existing = {r["source_url"] for r in await store.read(thread_id)}
    seen: set[str] = set()
    fetched = skipped = 0
    for url in urls:
        if fetched + skipped >= max_urls:
            break
        if not url or url in seen or url in existing:
            continue
        seen.add(url)
        text = await fetcher(url)
        if text:
            await store.record(thread_id, url, text, capture_status="raw_text")
            fetched += 1
        else:
            await store.record(thread_id, url, None, capture_status="skipped")
            skipped += 1
    return {"fetched": fetched, "skipped": skipped}
