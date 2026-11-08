"""LinkedIn image and document post resolver.

yt-dlp's LinkedInIE only scrapes the <video> tag out of a post's page - it
raises "Unable to extract video" for an image-only or document post (confirmed
live 2026-07-07). LinkedIn's public post pages server-render a plain og:image
meta tag for image posts and `data-native-document-config` for native document/
slide-deck (PDF) posts with no authentication needed when the post is public.

Some LinkedIn posts (or share links copied from the LinkedIn mobile app) redirect
anonymous requests to `/signup/cold-join` or `/authwall`, whose HTML carries
`og:title="Sign Up | LinkedIn"` and LinkedIn's own logo as `og:image`. We detect
that login wall (never mistaking the Sign Up page's logo for a post image), try
any signed-in `linkedin.com` browser/synced session (`li_at`), and if none can
open the post, raise `LinkedInAuthError` so the UI tells the user to sign in to
LinkedIn in their browser first.
"""

import gzip
import html as html_module
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import yt_dlp.extractor.linkedin as _ytdlp_linkedin
import yt_dlp.utils

from backend import config, cookies, extraction

LINKEDIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_OG_IMAGE_RE = re.compile(r'property="og:image"\s+content="([^"]+)"')
_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]+)"')
_DOC_CONFIG_RE = re.compile(r'data-native-document-config="([^"]+)"')
# On the logged-in flagship SDUI page, the document manifest sits in an escaped
# JSON string (`\"primaryManifestUrl\":\"https://media.licdn.com/dms/document/pl/...\"`)
# with `&` escaped as `\u0026`.
_LOGGED_IN_DOC_MANIFEST_RE = re.compile(
    r'\\?"primaryManifestUrl\\?"\s*:\s*\\?"(https://media\.licdn\.com/dms/document/pl/[^"\\\s]+(?:\\u0026[^"\\\s]+)*)\\?"'
)
_POST_SLUG_RE = re.compile(r"/posts/[^/_]+_(.+)-(?:ugcPost|activity|share)-\d+", re.IGNORECASE)

_LOGIN_WALL_PATHS = ("/signup", "/login", "/uas/login", "/authwall", "/checkpoint")
_LOGIN_WALL_TITLES = frozenset({
    "sign up | linkedin",
    "linkedin login, sign in | linkedin",
    "log in | linkedin",
    "sign in | linkedin",
    "linkedin: log in or sign up",
})

# On several regional ISPs (notably in Vietnam), TLS handshakes with
# SNI=www.linkedin.com / linkedin.com / vn.linkedin.com are dropped or stalled
# for 15-30s (`SSLEOFError` / handshake timeout), whereas LinkedIn's official
# Singapore web host `sg.linkedin.com` (same Cloudflare edge, same wildcard
# `*.linkedin.com` certificate, same HTML and `.linkedin.com` cookies) completes
# in ~0.1s without packet drops.
_THROTTLED_LINKEDIN_HOSTS = frozenset({"www.linkedin.com", "linkedin.com", "vn.linkedin.com"})
_LINKEDIN_MIRROR_HOST = "sg.linkedin.com"
_ORIG_URLOPEN = urllib.request.urlopen
_ANON_HTML_CACHE = {}  # url -> (expires_at, (html, final_url) | LinkedInAuthError)
_ANON_CACHE_TTL = 10.0


def _mirror_url(url):
    try:
        parsed = urllib.parse.urlparse(url or "")
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if host in _THROTTLED_LINKEDIN_HOSTS:
        return parsed._replace(netloc=_LINKEDIN_MIRROR_HOST).geturl()
    return url


def _is_login_wall_path(url):
    path = (urllib.parse.urlparse(url or "").path or "").lower()
    return any(path.startswith(prefix) for prefix in _LOGIN_WALL_PATHS)


class LinkedInUnsupportedPostError(Exception):
    """The post has nothing scrapable - a private/removed post, or a document
    post whose pages could not be resolved."""


class LinkedInAuthError(Exception):
    """LinkedIn redirected to its login/sign-up wall and no valid session could
    open the post."""


def _on_linkedin_cdn(url):
    # The manifests and page images all live on LinkedIn's CDN; anything else in
    # the page's JSON is ignored rather than fetched (this runs on a server).
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "licdn.com" or host.endswith(".licdn.com"))


def _is_static_logo(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host == "static.licdn.com" or host.startswith("static-")


def _is_login_wall(html, final_url=""):
    if _is_login_wall_path(final_url):
        return True
    title_match = _OG_TITLE_RE.search(html)
    if title_match:
        og_title = html_module.unescape(title_match.group(1)).strip().lower()
        if og_title in _LOGIN_WALL_TITLES:
            return True
    image_match = _OG_IMAGE_RE.search(html)
    if image_match and _is_static_logo(html_module.unescape(image_match.group(1))) and not _DOC_CONFIG_RE.search(html):
        return True
    return False


class _LinkedInRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _is_login_wall_path(newurl):
            raise LinkedInAuthError("LinkedIn requires a signed-in session to view this post.")
        return super().redirect_request(req, fp, code, msg, headers, _mirror_url(newurl))


_LINKEDIN_OPENER = urllib.request.build_opener(_LinkedInRedirectHandler)


def _cookie_rows(cookies_path):
    rows = []
    try:
        with open(cookies_path, "r", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return rows
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            if not line.startswith("#HttpOnly_"):
                continue
            line = line[len("#HttpOnly_"):]
        fields = line.split("\t")
        if len(fields) >= 7:
            domain = fields[0].lstrip(".").lower()
            if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
                rows.append((fields[5], fields[6]))
    return rows


def _has_session(cookies_path):
    return any(name == "li_at" and value for name, value in _cookie_rows(cookies_path))


def _cookie_header(cookies_path):
    # Forward only linkedin.com cookies from the jar.
    return "; ".join(f"{name}={value}" for name, value in _cookie_rows(cookies_path))


def linkedin_cookiefile_candidates():
    candidates = []
    manual = config.get_cookies_path()
    if manual and _has_session(manual):
        candidates.append(manual)
    candidates.extend(cookies.cookiefiles_from_browsers("linkedin.com", session_cookie="li_at"))
    return candidates


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": LINKEDIN_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _resolve_manifest_pages(manifest_url):
    if not _on_linkedin_cdn(manifest_url):
        return []
    resolutions = [
        r for r in (_fetch_json(manifest_url).get("perResolutions") or [])
        if _on_linkedin_cdn(r.get("imageManifestUrl"))
    ]
    if not resolutions:
        return []
    sharpest = max(resolutions, key=lambda r: r.get("width") or 0)
    return [
        p for p in (_fetch_json(sharpest["imageManifestUrl"]).get("pages") or [])
        if _on_linkedin_cdn(p)
    ]


def _document_pages(html):
    # None when the post is not a native document; otherwise (title, [page urls]),
    # with an EMPTY list when it is a document but its pages could not be read.
    match = _DOC_CONFIG_RE.search(html)
    if match:
        try:
            doc = json.loads(html_module.unescape(match.group(1))).get("doc") or {}
            manifest_url = doc.get("manifestUrl") or doc.get("url")
            return doc.get("title"), _resolve_manifest_pages(manifest_url)
        except (OSError, ValueError, KeyError, TypeError):
            return None, []

    sdui_match = _LOGGED_IN_DOC_MANIFEST_RE.search(html)
    if sdui_match:
        try:
            manifest_url = sdui_match.group(1).replace(r"\u0026", "&")
            return None, _resolve_manifest_pages(manifest_url)
        except (OSError, ValueError, KeyError, TypeError):
            return None, []

    return None


def _title_from_url(url):
    path = urllib.parse.urlparse(url or "").path or ""
    match = _POST_SLUG_RE.search(path)
    if not match:
        return None
    slug = match.group(1).replace("-", " ").strip()
    return slug.title() if slug else None


def _read_html_response(resp, default_url):
    raw = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower() if hasattr(resp, "headers") and resp.headers else ""
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    html = raw.decode("utf-8", "replace")
    final_url = resp.geturl() if hasattr(resp, "geturl") else default_url
    return html, final_url


def _fetch_html(url, cookies_path=None):
    headers = {
        "User-Agent": LINKEDIN_UA,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookies_path:
        cookie_hdr = _cookie_header(cookies_path)
        if cookie_hdr:
            headers["Cookie"] = cookie_hdr
        headers["Accept-Encoding"] = "gzip"

    # When unit tests monkeypatch urllib.request.urlopen, call it directly with
    # the original URL so test routers see the exact URL under test.
    if urllib.request.urlopen is not _ORIG_URLOPEN:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _read_html_response(resp, url)

    if not cookies_path:
        cached = _ANON_HTML_CACHE.get(url)
        if cached and cached[0] > time.time():
            if isinstance(cached[1], Exception):
                raise cached[1]
            return cached[1]

    target_url = _mirror_url(url)
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(target_url, headers=headers)
            with _LINKEDIN_OPENER.open(req, timeout=10) as resp:
                result = _read_html_response(resp, url)
            if not cookies_path:
                _ANON_HTML_CACHE[url] = (time.time() + _ANON_CACHE_TTL, result)
            return result
        except LinkedInAuthError as auth_err:
            if not cookies_path:
                _ANON_HTML_CACHE[url] = (time.time() + _ANON_CACHE_TTL, auth_err)
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            last_err = err
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise last_err


_ORIG_LINKEDIN_DOWNLOAD_WEBPAGE = _ytdlp_linkedin.LinkedInIE._download_webpage


def _patched_linkedin_download_webpage(self, url_or_request, video_id, *args, **kwargs):
    if isinstance(url_or_request, str) and urllib.request.urlopen is _ORIG_URLOPEN:
        try:
            html, _ = _fetch_html(url_or_request)
            return html
        except LinkedInAuthError as e:
            raise yt_dlp.utils.ExtractorError(str(e), expected=True) from e
        except Exception as e:
            raise yt_dlp.utils.ExtractorError(str(e), expected=True) from e
    return _ORIG_LINKEDIN_DOWNLOAD_WEBPAGE(self, url_or_request, video_id, *args, **kwargs)


_ytdlp_linkedin.LinkedInIE._download_webpage = _patched_linkedin_download_webpage


def _parse_post_html(html, url, final_url=""):
    if _is_login_wall(html, final_url):
        raise LinkedInAuthError("LinkedIn requires a signed-in session to view this post.")

    document = _document_pages(html)
    if document is not None:
        doc_title, pages = document
        if not pages:
            # A document's og:image is only its cover: returning that would
            # silently hand the user page 1 of N as if it were the whole post.
            raise LinkedInUnsupportedPostError(
                "This LinkedIn document post's pages could not be read."
            )
        title_match = _OG_TITLE_RE.search(html)
        title = (
            doc_title
            or (html_module.unescape(title_match.group(1)) if title_match else None)
            or _title_from_url(url)
            or "LinkedIn Document"
        )
        return {"title": title, "items": [{"kind": "image", "url": page, "thumbnail": page} for page in pages]}

    image_match = _OG_IMAGE_RE.search(html)
    if not image_match:
        raise LinkedInUnsupportedPostError(
            "No image found on this LinkedIn post - it may be a document/slide-deck post, "
            "which could not be read."
        )
    image_url = image_match.group(1).replace("&amp;", "&")

    title_match = _OG_TITLE_RE.search(html)
    title = html_module.unescape(title_match.group(1)) if title_match else (_title_from_url(url) or "LinkedIn Post")
    title = extraction.display_title("LinkedIn", title)

    return {"title": title, "items": [{"kind": "image", "url": image_url, "thumbnail": image_url}]}


def fetch_linkedin_image_post(url):
    # Returns {"title": str, "items": [{"kind": "image", "url", "thumbnail"}]}
    # - same shape as backend.instagram.fetch_instagram_media's single-item
    # case, so instagram.instagram_check_response can shape the /api/check
    # response unchanged. Tries anonymous first (fast guest HTML for public
    # posts); if LinkedIn redirects to its login/sign-up wall, falls back to
    # any available linkedin.com session cookie (`li_at`).
    try:
        html, final_url = _fetch_html(url)
        return _parse_post_html(html, url, final_url)
    except LinkedInAuthError as auth_err:
        candidates = linkedin_cookiefile_candidates()
        if not candidates:
            raise
        last_auth_error = auth_err
        last_error = None
        try:
            for cf in candidates:
                try:
                    html, final_url = _fetch_html(url, cookies_path=cf)
                    return _parse_post_html(html, url, final_url)
                except LinkedInAuthError as e:
                    last_auth_error = e
                except Exception as e:
                    last_error = e
        finally:
            cookies._cleanup_temp_cookiefiles(candidates)
        if last_error:
            raise last_error
        raise last_auth_error
