import asyncio
from open_deep_research.factbase import fetch

def test_html_to_text_strips_markup_and_scripts():
    html = "<html><head><style>x{}</style></head><body><script>bad()</script>" \
           "<h1>India</h1><p>coverage was 99% among adults</p></body></html>"
    txt = fetch.html_to_text(html)
    assert "coverage was 99% among adults" in txt
    assert "bad()" not in txt and "x{}" not in txt

def test_fetch_text_uses_injected_client_and_returns_text():
    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = b"<html><body><p>India coverage 99%</p></body></html>"
        text = "<html><body><p>India coverage 99%</p></body></html>"
    class _Client:
        async def get(self, url, **kw): return _Resp()
        async def aclose(self): pass
    out = asyncio.run(fetch.fetch_text("https://x.org/a", client=_Client()))
    assert "India coverage 99%" in out

def test_fetch_text_rejects_non_html_content_type():
    class _Resp:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        content = b"%PDF-1.4 ..."
        text = ""
    class _Client:
        async def get(self, url, **kw): return _Resp()
        async def aclose(self): pass
    assert asyncio.run(fetch.fetch_text("https://x.org/a.pdf", client=_Client())) is None

def test_fetch_text_returns_none_on_error():
    class _Client:
        async def get(self, url, **kw): raise RuntimeError("boom")
        async def aclose(self): pass
    assert asyncio.run(fetch.fetch_text("https://x.org/a", client=_Client())) is None
