"""Best-effort URL → readable text fetch for evidence backfill.

Used to independently retrieve the raw text of sources a run cited, so facts can
be span-verified against real source text rather than the model's summary.
NEVER raises: returns None on any failure (timeout, non-HTML, oversize, network).
"""
from __future__ import annotations
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_OK_TYPES = ("text/html", "text/plain", "application/xhtml")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return " ".join(text.split())


async def fetch_text(url: str, *, client=None, timeout: float = 10.0,
                     max_bytes: int = 2_000_000) -> str | None:
    if not (url or "").lower().startswith(("http://", "https://")):
        return None
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                   headers={"User-Agent": "open-deep-research-factbase/1.0"})
    try:
        resp = await client.get(url)
        if getattr(resp, "status_code", 0) != 200:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in _OK_TYPES):
            return None
        if len(getattr(resp, "content", b"") or b"") > max_bytes:
            return None
        text = fetch_text_from_response(resp)
        return text or None
    except Exception as e:
        logger.warning("fetch_text failed for %s: %s", url, e)
        return None
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass


def fetch_text_from_response(resp) -> str:
    return html_to_text(getattr(resp, "text", "") or "")
