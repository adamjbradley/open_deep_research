import asyncio, socket, ipaddress
import open_deep_research.factbase.fetch as fetch


def _patch_getaddrinfo(monkeypatch, ip):
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake)


def test_safe_host_rejects_metadata_ip(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "169.254.169.254")   # link-local (cloud metadata)
    assert fetch._safe_host("http://metadata.evil/latest") is False


def test_safe_host_rejects_loopback(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "127.0.0.1")
    assert fetch._safe_host("http://localhost-ish/") is False


def test_safe_host_rejects_private(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "10.0.0.5")
    assert fetch._safe_host("http://intranet/") is False


def test_safe_host_allows_public(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")     # example.com public
    assert fetch._safe_host("https://example.com/x") is True


def test_fetch_text_blocks_unsafe_host_before_request(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "127.0.0.1")
    class _Client:
        async def get(self, url, **kw): raise AssertionError("must not be called for unsafe host")
        async def aclose(self): pass
    assert asyncio.run(fetch.fetch_text("http://internal/", client=_Client())) is None


def test_fetch_text_blocks_redirect_to_internal(monkeypatch):
    # public host resolves fine, but server 302s to an internal URL -> must be blocked
    calls = {"n": 0}
    class _Resp:
        def __init__(self, status, headers=None, text=""):
            self.status_code = status; self.headers = headers or {}; self.text = text
            self.content = text.encode()
    class _Client:
        async def get(self, url, **kw):
            calls["n"] += 1
            return _Resp(302, {"location": "http://169.254.169.254/latest/meta-data/"})
        async def aclose(self): pass
    # make the redirect TARGET resolve to metadata IP, the original to public:
    def fake(host, *a, **k):
        ip = "169.254.169.254" if "169.254" in host else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake)
    out = asyncio.run(fetch.fetch_text("https://example.com/start", client=_Client()))
    assert out is None   # redirect to internal blocked


def test_fetch_connects_to_pinned_ip_with_host_and_sni(monkeypatch):
    # public host resolves to a fixed public IP; assert the GET targets the IP,
    # carries Host=hostname, and sni_hostname=hostname (so no re-resolution can rebind).
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake)
    seen = {}
    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"<p>India coverage 99%</p>"
        text = "<p>India coverage 99%</p>"
    class _Client:
        async def get(self, url, **kw):
            seen["url"] = url; seen["headers"] = kw.get("headers", {}); seen["ext"] = kw.get("extensions", {})
            return _Resp()
        async def aclose(self): pass
    out = asyncio.run(fetch.fetch_text("https://example.com/page", client=_Client()))
    assert "India coverage 99%" in out
    assert "93.184.216.34" in seen["url"]                 # connected to the pinned IP
    assert seen["headers"].get("Host") == "example.com"   # vhost preserved
    assert seen["ext"].get("sni_hostname") == "example.com"  # TLS verifies against hostname


def test_resolve_safe_returns_validated_ip(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")
    assert fetch._resolve_safe("https://example.com/x") == "93.184.216.34"


def test_resolve_safe_returns_none_for_private(monkeypatch):
    _patch_getaddrinfo(monkeypatch, "10.0.0.5")
    assert fetch._resolve_safe("http://intranet/") is None


def test_fetch_pins_ip_per_redirect_hop(monkeypatch):
    # first hop public IP A, server 302s to another public host that resolves to IP B;
    # assert each GET targets the IP it was resolved to (per-hop pinning).
    seen = []
    class _Resp:
        def __init__(self, status, headers=None, text=""):
            self.status_code = status; self.headers = headers or {}; self.text = text
            self.content = text.encode()
    class _Client:
        async def get(self, url, **kw):
            seen.append((url, kw.get("headers", {}).get("Host"), kw.get("extensions", {}).get("sni_hostname")))
            if len(seen) == 1:
                return _Resp(302, {"location": "https://second.example/final"})
            return _Resp(200, {"content-type": "text/html"}, "<p>done</p>")
        async def aclose(self): pass
    def fake(host, *a, **k):
        ip = "198.41.0.4" if "second" in host else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake)
    out = asyncio.run(fetch.fetch_text("https://first.example/start", client=_Client()))
    assert "done" in out
    assert len(seen) == 2
    assert "93.184.216.34" in seen[0][0] and seen[0][1] == "first.example" and seen[0][2] == "first.example"
    assert "198.41.0.4" in seen[1][0] and seen[1][1] == "second.example" and seen[1][2] == "second.example"


def test_fetch_pins_ipv6_in_brackets(monkeypatch):
    def fake(host, *a, **k):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0))]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", fake)
    seen = {}
    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"<p>ipv6 ok</p>"
        text = "<p>ipv6 ok</p>"
    class _Client:
        async def get(self, url, **kw):
            seen["url"] = url
            return _Resp()
        async def aclose(self): pass
    out = asyncio.run(fetch.fetch_text("https://v6.example/page", client=_Client()))
    assert "ipv6 ok" in out
    assert "[2606:2800:220:1:248:1893:25c8:1946]" in seen["url"]  # IPv6 literal bracketed
