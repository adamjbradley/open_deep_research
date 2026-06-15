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


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _resolve_safe(url: str) -> str | None:
    """SSRF guard: resolve the URL's host and reject if ANY resolved IP is
    private/loopback/link-local/reserved/multicast/unspecified (fail-closed).

    Returns the single validated public IP we resolved (as a string), or None
    if the host is unresolvable / unsafe. The caller connects to exactly this
    IP, which closes the DNS-rebinding TOCTOU window: httpx never re-resolves
    the name, so a hostile DNS server cannot swap in a private IP at connect
    time. We require every resolved address to be public, then pin the first
    one (which is among the validated set)."""
    try:
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
        if not host:
            return None
        infos = socket.getaddrinfo(host, None)
        if not infos:
            return None
        pinned: str | None = None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not _is_public_ip(ip):
                return None
            if pinned is None:
                pinned = str(ip)
        return pinned
    except Exception:
        return None


def _safe_host(url: str) -> bool:
    """Thin bool wrapper around `_resolve_safe` for callers/tests that only
    need a yes/no SSRF verdict."""
    return _resolve_safe(url) is not None


def _pin_url(url: str, ip: str):
    """Rewrite `url` so the netloc points at the validated `ip` (bracketing
    IPv6 literals and preserving any explicit port). Returns
    (ip_url, host_header, sni_hostname) where host_header/sni_hostname carry
    the original hostname so the virtual host and TLS certificate verification
    still bind to the real name, not the IP."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    netloc_ip = f"[{ip}]{port}" if ":" in ip else f"{ip}{port}"
    ip_url = parsed._replace(netloc=netloc_ip).geturl()
    return ip_url, parsed.netloc, host


async def fetch_text(url: str, *, client=None, timeout: float = 10.0,
                     max_bytes: int = 2_000_000, max_redirects: int = 5) -> str | None:
    if not (url or "").lower().startswith(("http://", "https://")):
        return None
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                   headers={"User-Agent": "open-deep-research-factbase/1.0"})
    try:
        current = url
        for _ in range(max_redirects + 1):
            # SSRF guard + DNS-rebinding fix: resolve ONCE and pin the validated
            # IP for this hop. Connecting to the IP literal means httpx does not
            # re-resolve the hostname at connect time, so a hostile DNS server
            # cannot return a public IP to the guard and a private IP to the
            # socket. The original hostname rides along as the Host header
            # (virtual host) and sni_hostname (TLS SNI + certificate verify).
            ip = _resolve_safe(current)
            if ip is None:
                return None
            ip_url, host_header, sni_host = _pin_url(current, ip)
            resp = await client.get(
                ip_url,
                headers={"Host": host_header},
                extensions={"sni_hostname": sni_host},
            )
            status = getattr(resp, "status_code", 0)
            if status in (301, 302, 303, 307, 308):
                location = (resp.headers.get("location") or "").strip()
                if not location:
                    return None
                new_url = urljoin(current, location)
                # Re-validate EACH hop: scheme + host must remain safe so a
                # public URL cannot redirect into the internal network. The next
                # loop iteration re-resolves and re-pins the redirect target.
                if not new_url.lower().startswith(("http://", "https://")):
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
