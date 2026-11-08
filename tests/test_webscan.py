import pytest

from backend import webscan as webscan_module
from backend.webscan import (
    WebScanBrowserUnavailableError,
    WebScanNoMediaError,
    WebScanTimeoutError,
    _items_from_scan_result,
    fetch_generic_page_media,
)


# ---- _items_from_scan_result (pure, no browser involved) ----


def test_collects_images_and_videos_with_absolute_urls():
    raw = {
        "title": "A Page",
        "images": [{"url": "https://site.com/photo.jpg", "width": 800, "height": 600}],
        "videos": [{"url": "https://site.com/clip.mp4", "poster": "https://site.com/poster.jpg"}],
        "network_media": [],
    }
    media = _items_from_scan_result(raw, "https://site.com/")
    assert media["title"] == "A Page"
    kinds = {item["kind"] for item in media["items"]}
    assert kinds == {"image", "video"}
    video = next(i for i in media["items"] if i["kind"] == "video")
    assert video["thumbnail"] == "https://site.com/poster.jpg"


def test_drops_tiny_images_but_keeps_images_with_unknown_size():
    raw = {
        "title": "T",
        "images": [
            {"url": "https://site.com/icon.png", "width": 16, "height": 16},
            {"url": "https://site.com/real.jpg", "width": 800, "height": 600},
            {"url": "https://site.com/unknown-size.jpg", "width": None, "height": None},
        ],
        "videos": [],
        "network_media": [],
    }
    urls = {item["url"] for item in _items_from_scan_result(raw, "https://site.com/")["items"]}
    assert "https://site.com/icon.png" not in urls
    assert "https://site.com/real.jpg" in urls
    assert "https://site.com/unknown-size.jpg" in urls


def test_drops_data_uris_and_dedupes_repeated_urls():
    raw = {
        "title": "T",
        "images": [
            {"url": "data:image/png;base64,abc", "width": None, "height": None},
            {"url": "https://site.com/a.jpg", "width": None, "height": None},
            {"url": "https://site.com/a.jpg", "width": None, "height": None},
        ],
        "videos": [],
        "network_media": [],
    }
    items = _items_from_scan_result(raw, "https://site.com/")["items"]
    assert len(items) == 1
    assert items[0]["url"] == "https://site.com/a.jpg"


def test_network_sniffed_media_is_included_and_typed_by_content_type():
    raw = {
        "title": "T",
        "images": [],
        "videos": [],
        "network_media": [
            {"url": "https://cdn.site.com/sniffed.mp4", "content_type": "video/mp4"},
            {"url": "https://cdn.site.com/sniffed.jpg", "content_type": "image/jpeg"},
        ],
    }
    items = _items_from_scan_result(raw, "https://site.com/")["items"]
    by_url = {item["url"]: item["kind"] for item in items}
    assert by_url["https://cdn.site.com/sniffed.mp4"] == "video"
    assert by_url["https://cdn.site.com/sniffed.jpg"] == "image"


def test_og_image_used_only_when_dom_scan_found_no_images():
    raw_with_images = {
        "title": "T", "og_image": "https://site.com/banner.jpg",
        "images": [{"url": "https://site.com/real.jpg", "width": None, "height": None}],
        "videos": [], "network_media": [],
    }
    urls = {i["url"] for i in _items_from_scan_result(raw_with_images, "https://site.com/")["items"]}
    assert "https://site.com/banner.jpg" not in urls

    raw_without_images = {"title": "T", "og_image": "https://site.com/banner.jpg", "images": [], "videos": [], "network_media": []}
    urls = {i["url"] for i in _items_from_scan_result(raw_without_images, "https://site.com/")["items"]}
    assert "https://site.com/banner.jpg" in urls


def test_og_video_is_always_added():
    raw = {"title": "T", "og_video": "https://site.com/trailer.mp4", "images": [], "videos": [], "network_media": []}
    items = _items_from_scan_result(raw, "https://site.com/")["items"]
    assert items == [{"kind": "video", "url": "https://site.com/trailer.mp4", "thumbnail": "https://site.com/trailer.mp4"}]


def test_title_falls_back_through_og_title_then_title_then_webpage():
    raw = {"images": [{"url": "https://site.com/a.jpg", "width": None, "height": None}], "videos": [], "network_media": []}
    assert _items_from_scan_result(raw, "https://site.com/")["title"] == "Webpage"
    raw["title"] = "Plain Title"
    assert _items_from_scan_result(raw, "https://site.com/")["title"] == "Plain Title"
    raw["og_title"] = "OG Title"
    assert _items_from_scan_result(raw, "https://site.com/")["title"] == "OG Title"


# ---- fetch_generic_page_media orchestration (browser mocked) ----


def test_fetch_generic_page_media_raises_no_media_when_nothing_found(monkeypatch):
    monkeypatch.setattr(webscan_module, "_run_browser_scan", lambda url, timeout: {
        "title": "Empty", "images": [], "videos": [], "network_media": [], "base_url": url,
    })
    with pytest.raises(WebScanNoMediaError):
        fetch_generic_page_media("https://site.com/empty")


def test_fetch_generic_page_media_returns_items_shape_other_resolvers_expect(monkeypatch):
    monkeypatch.setattr(webscan_module, "_run_browser_scan", lambda url, timeout: {
        "title": "A Gallery",
        "images": [{"url": "https://site.com/1.jpg", "width": 800, "height": 600}],
        "videos": [],
        "network_media": [],
        "base_url": url,
    })
    media = fetch_generic_page_media("https://site.com/gallery")
    assert media == {
        "title": "A Gallery",
        "items": [{"kind": "image", "url": "https://site.com/1.jpg", "thumbnail": "https://site.com/1.jpg"}],
    }


def test_fetch_generic_page_media_propagates_timeout(monkeypatch):
    def raise_timeout(url, timeout):
        raise WebScanTimeoutError("too slow")

    monkeypatch.setattr(webscan_module, "_run_browser_scan", raise_timeout)
    with pytest.raises(WebScanTimeoutError):
        fetch_generic_page_media("https://site.com/slow")


def test_fetch_generic_page_media_propagates_browser_unavailable(monkeypatch):
    def raise_unavailable(url, timeout):
        raise WebScanBrowserUnavailableError("no chromium")

    monkeypatch.setattr(webscan_module, "_run_browser_scan", raise_unavailable)
    with pytest.raises(WebScanBrowserUnavailableError):
        fetch_generic_page_media("https://site.com/no-browser")


def test_warm_up_chromium_never_raises_even_when_browser_unavailable(monkeypatch):
    def raise_unavailable():
        raise WebScanBrowserUnavailableError("no network")

    monkeypatch.setattr(webscan_module, "_ensure_chromium", raise_unavailable)
    webscan_module.warm_up_chromium()  # must not raise
