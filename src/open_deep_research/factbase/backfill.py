"""Backfill run_source by independently fetching cited URLs' raw text.

Decouples discovery (whatever search the run used) from evidence capture, so facts
can be span-verified against independently-fetched source text on ANY search backend.
"""
from __future__ import annotations


async def backfill_run_sources(store, thread_id: str, urls: list[str], fetcher,
                               *, max_urls: int = 20) -> dict:
    existing_rows = await store.read(thread_id)
    # Track existing status to allow upgrading skipped/summarized to raw_text.
    existing_status = {r["source_url"]: r["capture_status"] for r in existing_rows}
    
    seen: set[str] = set()
    fetched = skipped = 0
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        
        # If we already have raw_text for this thread, no need to re-fetch or re-record.
        if existing_status.get(url) == "raw_text":
            continue

        if fetched + skipped >= max_urls:
            # Only record 'skipped' if we don't have something better already.
            if url not in existing_status:
                await store.record(thread_id, url, None, capture_status="skipped", reason="source_ceiling")
            skipped += 1
            continue

        text = await fetcher(url)
        if text:
            await store.record(thread_id, url, text, capture_status="raw_text")
            fetched += 1
        else:
            # Only record 'skipped' if we don't have something better already.
            if url not in existing_status:
                await store.record(thread_id, url, None, capture_status="skipped")
            skipped += 1
    return {"fetched": fetched, "skipped": skipped}
