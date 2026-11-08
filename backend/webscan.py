"""Generic website media scanner - desktop-app-only, last-resort fallback.

Every other resolver in this package targets ONE named platform whose page shape is
known in advance (LinkedIn's og:image, TikTok's tikwm.com API, ...). This module is
different on purpose: it is the fallback for a URL that matched none of them (classify.py's
"Link" platform - see get_platform_info's fall-through) AND that yt-dlp's own generic
extractor also failed to resolve. At that point the URL is some arbitrary webpage, so
instead of giving up we render it in a real headless browser and list every image/video
the page actually shows, the same way a person would Save Image/Save Video by hand.

Why a real browser and not just an HTML parse: many sites only populate their images via
client-side JS (SPAs, lazy-loaded galleries), so a plain HTML fetch sees an almost-empty
page. A headless Chromium renders exactly what a visitor sees, including JS-injected
content, at the cost of being much slower (seconds, not milliseconds) and far heavier
(a real browser process) than every other resolver here - which is exactly why this is
gated local-only (see backend/app.py's is_local_request() check at the one call site) and
never wired into remote_web: a browser process per request would risk taking down the
production e2-micro VM under concurrent visitors.

Hard, accepted limitation: this reads the DOM (img/video/source elements, og:image/
og:video meta tags) plus network responses whose content-type is image/*/video/* seen
while the page loads. It cannot resolve a `blob:` object URL (the bytes only exist in the
browser tab's memory, not as a fetchable URL) or stitch an HLS/DASH stream's many segment
URLs into one file - most JS video-player widgets (Vimeo-style custom players) fall into
one of those two buckets and will not produce a downloadable item. Plain <img> photos and
progressive direct-file (<video src="*.mp4">) videos are what this reliably catches.
"""

import subprocess
import sys
import urllib.parse

_MAX_ITEMS = 150
_MIN_DIMENSION = 80  # drop an <img> this small or smaller when its size is known - favicons/trackers, not content
_SCROLL_PASSES = 6
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The in-page JS that does the actual DOM scan. Kept as one string (not several
# page.evaluate calls) so it runs as a single atomic snapshot of the rendered DOM.
_SCAN_JS = """
() => {
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch (e) { return null; } };
  const pickFromSrcset = (srcset) => {
    if (!srcset) return null;
    // "url1 100w, url2 300w" / "url1 1x, url2 2x" - take the last (yt-dlp-style: widest/densest wins).
    const candidates = srcset.split(",").map((s) => s.trim().split(/\\s+/)[0]).filter(Boolean);
    return candidates.length ? candidates[candidates.length - 1] : null;
  };
  const images = [];
  document.querySelectorAll("img").forEach((img) => {
    const src = pickFromSrcset(img.getAttribute("srcset")) || img.currentSrc || img.getAttribute("src");
    if (!src) return;
    images.push({
      url: abs(src),
      width: img.naturalWidth || img.width || null,
      height: img.naturalHeight || img.height || null,
    });
  });
  document.querySelectorAll("picture source[srcset]").forEach((source) => {
    const src = pickFromSrcset(source.getAttribute("srcset"));
    if (src) images.push({ url: abs(src), width: null, height: null });
  });
  const videos = [];
  document.querySelectorAll("video").forEach((video) => {
    const sources = [video.getAttribute("src")]
      .concat(Array.from(video.querySelectorAll("source")).map((s) => s.getAttribute("src")));
    const poster = video.getAttribute("poster");
    sources.filter(Boolean).forEach((src) => {
      videos.push({ url: abs(src), poster: poster ? abs(poster) : null });
    });
  });
  const metaContent = (name) => {
    const el = document.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
    return el ? el.getAttribute("content") : null;
  };
  return {
    title: document.title || null,
    og_title: metaContent("og:title"),
    og_image: metaContent("og:image") || metaContent("twitter:image"),
    og_video: metaContent("og:video"),
    images,
    videos,
    base_url: document.baseURI,
  };
}
"""


class WebScanError(Exception):
    """Base class for every error this module raises."""


class WebScanNoMediaError(WebScanError):
    """The page rendered fine but had nothing worth listing."""


class WebScanTimeoutError(WebScanError):
    """The page never finished loading within the time budget."""


class WebScanBrowserUnavailableError(WebScanError):
    """Chromium isn't installed and the one-time auto-install failed."""


def _is_missing_browser_error(err):
    # Playwright's own message for "playwright install" never having run -
    # matched by text since Playwright doesn't expose a dedicated exception
    # type for this specific case.
    return "executable doesn't exist" in str(err).lower()


def _ensure_chromium():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise WebScanBrowserUnavailableError("The playwright package is not installed.") from e

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return
    except Exception as e:
        if not _is_missing_browser_error(e):
            raise WebScanBrowserUnavailableError(str(e)) from e

    # Chromium was never downloaded - fetch it once (~150MB, needs network).
    # desktop_app.py normally does this in the background at startup so a
    # user's first real scan doesn't pay for it, but this covers dev mode
    # (python3 server.py) and a cold first call either way.
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True, capture_output=True, timeout=300,
        )
    except Exception as e:
        raise WebScanBrowserUnavailableError(
            "Couldn't download the browser engine needed to scan this page (chromium)."
        ) from e


def warm_up_chromium():
    # Fire-and-forget convenience for desktop_app.py's startup thread - never
    # raises, since a failed warm-up just means the first real scan pays the
    # install cost (or surfaces WebScanBrowserUnavailableError) instead.
    try:
        _ensure_chromium()
    except WebScanBrowserUnavailableError:
        pass


def _run_browser_scan(url, timeout):
    _ensure_chromium()
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    network_media = []

    def on_response(response):
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type.startswith("image/") or content_type.startswith("video/"):
            network_media.append({"url": response.url, "content_type": content_type})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_DESKTOP_UA, viewport={"width": 1280, "height": 1600})
                page.on("response", on_response)
                page.goto(url, wait_until="load", timeout=timeout * 1000)
                # Trigger lazy-loaded images/infinite-scroll content.
                for _ in range(_SCROLL_PASSES):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(400)
                page.evaluate("window.scrollTo(0, 0)")
                result = page.evaluate(_SCAN_JS)
            finally:
                browser.close()
    except PlaywrightTimeoutError as e:
        raise WebScanTimeoutError(f"Timed out loading {url}") from e
    except Exception as e:
        if _is_missing_browser_error(e):
            raise WebScanBrowserUnavailableError(str(e)) from e
        raise

    result["network_media"] = network_media
    return result


def _items_from_scan_result(raw, base_url):
    # Pure function, no I/O - this is what tests/test_webscan.py exercises
    # directly with hand-built fake scan payloads.
    seen = set()
    items = []

    def add(url, kind, thumbnail=None):
        if not url or len(items) >= _MAX_ITEMS:
            return
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return
        if parsed.scheme not in ("http", "https"):
            return  # drops data: URIs and anything else not independently fetchable
        if url in seen:
            return
        seen.add(url)
        items.append({"kind": kind, "url": url, "thumbnail": thumbnail or url})

    for image in raw.get("images") or []:
        width, height = image.get("width"), image.get("height")
        if width and height and (width < _MIN_DIMENSION or height < _MIN_DIMENSION):
            continue
        add(image.get("url"), "image")

    for video in raw.get("videos") or []:
        add(video.get("url"), "video", thumbnail=video.get("poster"))

    for media in raw.get("network_media") or []:
        kind = "video" if media["content_type"].startswith("video/") else "image"
        add(media.get("url"), kind)

    if raw.get("og_video"):
        add(raw["og_video"], "video")
    if raw.get("og_image") and not any(i["kind"] == "image" for i in items):
        # The og:image is usually also one of the page's own <img> tags; only
        # add it if the DOM scan found no images at all, to avoid listing a
        # generic social-share banner alongside the page's real content.
        add(raw["og_image"], "image")

    title = raw.get("og_title") or raw.get("title") or "Webpage"
    return {"title": title, "items": items}


def fetch_generic_page_media(url, timeout=25):
    # Returns {"title": str, "items": [{"kind": "image"|"video", "url", "thumbnail"}]}
    # - same shape backend.instagram.instagram_check_response already expects from
    # every other resolver in this package.
    raw = _run_browser_scan(url, timeout)
    media = _items_from_scan_result(raw, raw.get("base_url") or url)
    if not media["items"]:
        raise WebScanNoMediaError(f"No image or video content found on {url}")
    return media
