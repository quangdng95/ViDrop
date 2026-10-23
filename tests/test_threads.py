"""Unit tests for backend.threads - the Threads post resolver.

Mirrors tests/test_instagram.py's structure: Threads shares Instagram's
private REST API shape (confirmed live 2026-07-07 against two real owner
posts - one video, one image), so the resolver's own logic (shortcode decode,
auth-error mapping, item shaping) is tested the same way.
"""

import json
import urllib.error
import urllib.request

import pytest

from backend import threads as threads_module
from backend.threads import ThreadsAuthError, threads_media_id_from_shortcode


def test_threads_media_id_from_shortcode_matches_instagram_scheme():
    # Confirmed live: Threads shortcodes decode with the exact same url-safe
    # base64 alphabet Instagram media ids use.
    from backend.instagram import instagram_media_id_from_shortcode
    for code in ("DWE8-rMEmXp", "DaTf9pqiaMW"):
        assert threads_media_id_from_shortcode(code) == instagram_media_id_from_shortcode(code)


def test_parse_threads_cookies_filters_by_threads_domain(tmp_path):
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(
        "#HttpOnly_.threads.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\tsess123\n"
        ".threads.com\tTRUE\t/\tTRUE\t1799999999\tcsrftoken\tcsrf456\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\tignoreme\n"
    )
    cookies = threads_module._parse_threads_cookies(str(cookies_file))
    assert cookies["sessionid"] == "sess123"
    assert cookies["csrftoken"] == "csrf456"


def test_fetch_threads_media_maps_login_redirect_to_auth_error(tmp_path, monkeypatch):
    # Mirrors the Instagram 302-redirect test - an invalid/expired session
    # redirects to login rather than returning 401/403.
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(".threads.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\tbad\n")

    def raise_302(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_302)
    with pytest.raises(ThreadsAuthError):
        threads_module.fetch_threads_media("https://www.threads.com/@someone/post/DWE8-rMEmXp", str(cookies_file))


def test_fetch_threads_media_maps_html_response_to_auth_error(tmp_path, monkeypatch):
    # An unauthenticated request gets served the SPA shell (HTML), not JSON -
    # confirmed live 2026-07-07, distinct from a clean HTTP error status.
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(".threads.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\tbad\n")

    class _HtmlResponse:
        def read(self):
            return b"<!DOCTYPE html><html>...</html>"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: _HtmlResponse())
    with pytest.raises(ThreadsAuthError):
        threads_module.fetch_threads_media("https://www.threads.com/@someone/post/DWE8-rMEmXp", str(cookies_file))


def test_fetch_threads_media_parses_video_item(tmp_path, monkeypatch):
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(".threads.com\tTRUE\t/\tTRUE\t1799999999\tcsrftoken\tabc\n")

    payload = {
        "items": [{
            "code": "DWE8-rMEmXp",
            "caption": {"text": "Almost finished!\nmore text"},
            "video_versions": [{"url": "https://cdn/video.mp4", "width": 1280, "height": 706}],
            "image_versions2": {"candidates": [{"url": "https://cdn/thumb.jpg"}]},
        }],
    }

    class _JsonResponse:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: _JsonResponse())
    media = threads_module.fetch_threads_media("https://www.threads.com/@someone/post/DWE8-rMEmXp", str(cookies_file))
    assert media["title"] == "Almost finished!"
    assert media["items"] == [{
        "kind": "video", "url": "https://cdn/video.mp4", "thumbnail": "https://cdn/thumb.jpg",
        "width": 1280, "height": 706,
    }]


def test_fetch_threads_media_parses_image_item(tmp_path, monkeypatch):
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(".threads.com\tTRUE\t/\tTRUE\t1799999999\tcsrftoken\tabc\n")

    payload = {
        "items": [{
            "code": "DaTf9pqiaMW",
            "caption": {"text": "Signs everywhere"},
            "image_versions2": {"candidates": [{"url": "https://cdn/full.jpg", "width": 3024, "height": 4032}]},
        }],
    }

    class _JsonResponse:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: _JsonResponse())
    media = threads_module.fetch_threads_media("https://www.threads.com/@figma/post/DaTf9pqiaMW", str(cookies_file))
    assert media["items"] == [{
        "kind": "image", "url": "https://cdn/full.jpg", "thumbnail": "https://cdn/full.jpg",
        "width": 3024, "height": 4032,
    }]


def test_fetch_threads_media_any_returns_first_account_that_works(monkeypatch):
    calls = []

    def fake_fetch(url, cf):
        calls.append(cf)
        if cf == "/acctA.txt":
            raise ThreadsAuthError("Threads requires a logged-in session (cookies).")
        return {"title": "Post", "items": [{"kind": "video", "url": "http://cdn/v.mp4", "thumbnail": None}]}

    monkeypatch.setattr(threads_module, "fetch_threads_media", fake_fetch)
    media = threads_module.fetch_threads_media_any("https://www.threads.com/@someone/post/abc", ["/acctA.txt", "/acctB.txt"])
    assert media["title"] == "Post"
    assert calls == ["/acctA.txt", "/acctB.txt"]


def test_fetch_threads_media_any_raises_auth_error_when_all_unauthorized(monkeypatch):
    def always_auth(url, cf):
        raise ThreadsAuthError("Threads requires a logged-in session (cookies).")

    monkeypatch.setattr(threads_module, "fetch_threads_media", always_auth)
    with pytest.raises(ThreadsAuthError):
        threads_module.fetch_threads_media_any("https://www.threads.com/@someone/post/abc", ["/a.txt", "/b.txt"])


# ---- threads_cookiefile_candidates: the uploaded cookies.txt (2026-10-03) ----
#
# On the headless cloud VM there is no browser to extract from, so the ONLY
# Threads session it can have is the cookies.txt the Mac syncs up. Instagram
# already consulted that file (backend.cookies.instagram_cookiefile_candidates);
# Threads did not, so every Threads link on the cloud failed with "needs a
# logged-in browser on this machine" even though the uploaded jar carried a
# threads.com sessionid.

THREADS_JAR = ".threads.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\tabc\n"
INSTAGRAM_ONLY_JAR = ".instagram.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\tabc\n"


def test_threads_candidates_include_the_uploaded_cookie_file_when_it_has_a_threads_session(monkeypatch, tmp_path):
    jar = tmp_path / "manual.txt"
    jar.write_text(THREADS_JAR)
    monkeypatch.setattr(threads_module.config, "get_cookies_path", lambda: str(jar))
    monkeypatch.setattr(threads_module.cookies, "cookiefiles_from_browsers", lambda domain="": ["/tmp/omniflow-cookies-browser.txt"])

    # The manual file comes first and is never one of the temp files that get cleaned up.
    assert threads_module.threads_cookiefile_candidates() == [str(jar), "/tmp/omniflow-cookies-browser.txt"]


def test_threads_candidates_skip_an_uploaded_file_with_no_threads_session(monkeypatch, tmp_path):
    jar = tmp_path / "manual.txt"
    jar.write_text(INSTAGRAM_ONLY_JAR)
    monkeypatch.setattr(threads_module.config, "get_cookies_path", lambda: str(jar))
    monkeypatch.setattr(threads_module.cookies, "cookiefiles_from_browsers", lambda domain="": [])
    assert threads_module.threads_cookiefile_candidates() == []


def test_threads_candidates_work_with_no_uploaded_file(monkeypatch):
    monkeypatch.setattr(threads_module.config, "get_cookies_path", lambda: None)
    monkeypatch.setattr(threads_module.cookies, "cookiefiles_from_browsers", lambda domain="": ["/tmp/b.txt"])
    assert threads_module.threads_cookiefile_candidates() == ["/tmp/b.txt"]


# ---- the public page path (2026-10: the private API redirects even a valid session to the login page) ----

import types

_POST = {"code": "Abc123", "caption": {"text": "Hello world\nsecond line"}, "image_versions2": {"candidates": [{"url": "https://cdn/i.jpg", "width": 640, "height": 640}]}}
_VIDEO = {"code": "Vid456", "image_versions2": {"candidates": [{"url": "https://cdn/t.jpg", "width": 10, "height": 10}]}, "video_versions": [{"url": "https://cdn/v.mp4", "width": 720, "height": 1280}]}
_CAROUSEL = {"code": "Car789", "carousel_media": [_POST, _VIDEO]}


def _page(*posts, noise=True):
    blobs = ['<script type="application/json" data-sjs>{"other":{"code":"Zzz","video_versions":[{"url":"https://cdn/NOPE.mp4"}]}}</script>'] if noise else []
    blobs += [f'<script type="application/json" data-sjs>{json.dumps({"x": [{"y": post}]})}</script>' for post in posts]
    return "<html>" + "".join(blobs) + "</html>"


def _fake_session(monkeypatch, html, status=200):
    class Session:
        def __init__(self, **kw):
            pass

        def get(self, url, **kw):
            return types.SimpleNamespace(status_code=status, text=html)

    monkeypatch.setattr(threads_module.curl_requests, "Session", Session)


def test_page_image_post(monkeypatch):
    _fake_session(monkeypatch, _page(_POST))
    media = threads_module.fetch_threads_page("https://www.threads.com/@u/post/Abc123")
    assert media["title"] == "Hello world"
    assert media["items"][0]["kind"] == "image" and media["items"][0]["url"] == "https://cdn/i.jpg"


def test_page_video_post_picks_the_right_post_not_the_feed_noise(monkeypatch):
    _fake_session(monkeypatch, _page(_VIDEO))
    media = threads_module.fetch_threads_page("https://www.threads.com/@u/post/Vid456")
    assert [i["kind"] for i in media["items"]] == ["video"] and media["items"][0]["url"] == "https://cdn/v.mp4"


def test_page_carousel_lists_every_slide(monkeypatch):
    _fake_session(monkeypatch, _page(_CAROUSEL))
    media = threads_module.fetch_threads_page("https://www.threads.com/@u/post/Car789")
    assert [i["kind"] for i in media["items"]] == ["image", "video"]


@pytest.mark.parametrize("html, status", [("<html>login</html>", 200), (_page(_POST), 404), ("", 200)])
def test_page_without_the_post_is_an_auth_error(monkeypatch, html, status):
    _fake_session(monkeypatch, html, status)
    with pytest.raises(ThreadsAuthError):
        threads_module.fetch_threads_page("https://www.threads.com/@u/post/Abc123")


def test_post_uses_the_page_first_and_never_reads_cookies(monkeypatch):
    monkeypatch.setattr(threads_module, "fetch_threads_page", lambda url: {"title": "t", "items": []})

    def boom():
        raise AssertionError("cookies must not be read when the public page works")

    assert threads_module.fetch_threads_post("https://www.threads.com/@u/post/Abc123", get_candidates=boom)["title"] == "t"


def test_post_falls_back_to_the_login_when_the_page_hides_it(monkeypatch):
    def hidden(url):
        raise ThreadsAuthError("hidden")

    monkeypatch.setattr(threads_module, "fetch_threads_page", hidden)
    monkeypatch.setattr(threads_module, "fetch_threads_media_any", lambda url, cfs: {"title": "private", "items": [], "via": cfs})
    cleaned = []
    monkeypatch.setattr(threads_module.cookies, "_cleanup_temp_cookiefiles", lambda c: cleaned.append(list(c)))
    media = threads_module.fetch_threads_post("https://www.threads.com/@u/post/Abc123", get_candidates=lambda: ["/a.txt"])
    assert media["title"] == "private" and cleaned == [["/a.txt"]]
