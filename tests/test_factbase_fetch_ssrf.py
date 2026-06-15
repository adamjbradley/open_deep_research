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
