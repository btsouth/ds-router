"""HTTP policy for requests carrying provider credentials."""

import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even an HTTPS origin can redirect to a different host or plaintext URL.
        return None


def open_request(request, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    return opener.open(request, timeout=timeout)


def safe_error(exc):
    """Remote reason phrases and exception details are not safe log content."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError: HTTP {exc.code}"
    return type(exc).__name__
