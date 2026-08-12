"""
SSRF-safe URL fetcher for audio files.
Validates target hostname before making any request, and re-validates
every redirect hop so a redirect to an internal address is rejected.
"""
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

ALLOWED_SCHEMES = ('http', 'https')

# Cap on how much audio we'll download from a URL (100 MB, matching the
# upload limit). Prevents a hostile/broken URL from exhausting memory.
MAX_FETCH_BYTES = 100 * 1024 * 1024


def _is_public_ip(ip_str):
    """Return True if an IP literal is a routable, public address."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) must be judged by the v4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_public_ip(str(ip.ipv4_mapped))
    if (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
        return False
    return True


def is_safe_url(url):
    """
    Validate a URL is safe to fetch.
    Returns (True, hostname) or (False, reason).
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, 'Only http/https URLs allowed'
    hostname = parsed.hostname or ''
    if not hostname:
        return False, 'Invalid URL: no hostname'
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False, 'Cannot resolve hostname'
    if not infos or not any(_is_public_ip(info[4][0]) for info in infos):
        return False, 'Internal addresses not allowed'
    return True, hostname


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects that point at internal/non-public hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe, reason = is_safe_url(newurl)
        if not safe:
            raise urllib.error.HTTPError(
                newurl, code, f'Redirect blocked: {reason}', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_audio(url, save_callback):
    """
    Fetch an audio file from a validated URL.
    Uses save_callback(data, ext) to write the data so this module
    never constructs file paths directly.
    Returns the content-type. Raises ValueError if unsafe.
    """
    safe, reason = is_safe_url(url)
    if not safe:
        raise ValueError(reason)
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    resp = opener.open(
        urllib.request.Request(url, headers={'User-Agent': 'MelodySuite/1.0'}),
        timeout=15,
    )
    data = resp.read(MAX_FETCH_BYTES + 1)
    if len(data) > MAX_FETCH_BYTES:
        raise ValueError('File too large (max 100MB)')
    ct = resp.headers.get('Content-Type', '').lower()
    if 'wav' in ct:
        ext = '.wav'
    elif 'ogg' in ct:
        ext = '.ogg'
    elif 'flac' in ct:
        ext = '.flac'
    else:
        ext = '.mp3'
    save_callback(data, ext)
    return ct
