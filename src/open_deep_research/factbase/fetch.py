"""Best-effort URL → readable text fetch for evidence backfill.

Used to independently retrieve the raw text of sources a run cited, so facts can
be span-verified against real source text rather than the model's summary.
NEVER raises: returns None on any failure (timeout, non-HTML, oversize, network).
"""
from __future__ import annotations
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_OK_TYPES = ("text/html", "text/plain", "application/xhtml")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return " ".join(text.split())


def _safe_host(url: str) -> bool:
    """SSRF guard: resolve the URL's host and reject if any resolved IP is
    private/loopback/link-local/reserved/multicast/unspecified. Returns True
    only when the host resolves and every resolved address is public."""
    try:
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
        if not host:
            return False
        infos = socket.getaddrinfo(host, None)
        if not infos:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


async def fetch_text(url: str, *, client=None, timeout: float = 10.0,
                     max_bytes: int = 2_000_000, max_redirects: int = 5) -> str | None:
    if not (url or "").lower().startswith(("http://", "https://")):
        return None
    # SSRF guard: validate the initial host before issuing any request.
    if not _safe_host(url):
        return None
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                   headers={"User-Agent": "open-deep-research-factbase/1.0"})
    try:
        current = url
        for _ in range(max_redirects + 1):
            resp = await client.get(current)
            status = getattr(resp, "status_code", 0)
            if status in (301, 302, 303, 307, 308):
                location = (resp.headers.get("location") or "").strip()
                if not location:
                    return None
                new_url = urljoin(current, location)
                # Re-validate EACH hop: scheme + host must remain safe so a
                # public URL cannot redirect into the internal network.
                if not new_url.lower().startswith(("http://", "https://")):
                    return None
                if not _safe_host(new_url):
                    return None
                current = new_url
                continue
            if status != 200:
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if not any(t in ctype for t in _OK_TYPES):
                return None
            if len(getattr(resp, "content", b"") or b"") > max_bytes:
                return None
            text = fetch_text_from_response(resp)
            return text or None
        # Redirect budget exhausted.
        return None
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
