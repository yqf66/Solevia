"""Browser boundary for the loopback API; loopback binding alone is not authentication."""
import hmac
import os
import secrets

from starlette.responses import JSONResponse


class LocalAPISecurity:
    def __init__(self, app, *, token=None, origins=None):
        self.app = app
        self.token = token or os.environ.get("SOLEVIA_SESSION_TOKEN") or secrets.token_urlsafe(32)
        self.origins = set(origins or os.environ.get(
            "SOLEVIA_FRONTEND_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
        ).split(","))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        duplicate = False
        for key, value in scope["headers"]:
            name = key.decode("latin1").lower()
            if name in headers and name in {"host", "origin", "x-solevia-token"}:
                duplicate = True
            headers[name] = value.decode("latin1")
        origin = headers.get("origin")
        site = headers.get("sec-fetch-site")
        if duplicate or (origin is not None and origin not in self.origins) or (
            site is not None and site not in {"same-origin", "none"}
        ):
            return await JSONResponse({"detail": "Untrusted local request"}, 403)(scope, receive, send)
        path = scope["path"]
        if path == "/api/health" and scope["method"] in {"GET", "HEAD"}:
            return await self.app(scope, receive, send)
        if path == "/api/session" and scope["method"] == "GET":
            # Cross-origin JS cannot add this header without a successful CORS
            # preflight. This service deliberately does not enable CORS.
            if headers.get("x-solevia-bootstrap") != "1":
                return await JSONResponse({"detail": "Session bootstrap required"}, 403)(scope, receive, send)
            return await JSONResponse({"token": self.token}, headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            })(scope, receive, send)
        supplied = headers.get("x-solevia-token", "")
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            return await JSONResponse({"detail": "Local session expired", "code": "LOCAL_SESSION_REQUIRED"}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)
