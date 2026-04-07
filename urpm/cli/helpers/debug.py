"""Debug utilities for CLI operations."""


def notify_urpmd_cache_invalidate():
    """Notify local urpmd to invalidate its RPM cache index.

    This allows newly downloaded packages to be visible to peer queries.
    Tries both dev and prod ports silently.
    """
    import urllib.request
    import urllib.error
    from ...core.config import DEV_PORT, PROD_PORT

    ports = [DEV_PORT, PROD_PORT]

    for port in ports:
        try:
            url = f"http://127.0.0.1:{port}/api/invalidate-cache"
            req = urllib.request.Request(url, method='POST')
            req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.status == 200:
                    return  # Success, no need to try other port
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue  # Try next port or give up silently


# Backwards compatibility alias (with underscore prefix)
_notify_urpmd_cache_invalidate = notify_urpmd_cache_invalidate
