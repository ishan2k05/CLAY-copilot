import os
import json
import secrets
import hmac
from pathlib import Path
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

def resolve_or_create_session_token() -> str:
    """Resolve active session token from environment or ~/.clay/auth_token.json."""
    env_token = os.environ.get("CLAY_SESSION_TOKEN", "").strip()
    if env_token:
        return env_token

    clay_dir = Path.home() / ".clay"
    auth_file = clay_dir / "auth_token.json"
    try:
        if auth_file.exists():
            data = json.loads(auth_file.read_text(encoding="utf-8"))
            token = data.get("token", "").strip()
            if token:
                return token
    except Exception:
        pass

    token = secrets.token_hex(32)
    try:
        clay_dir.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(json.dumps({"token": token}), encoding="utf-8")
        try:
            auth_file.chmod(0o600)
        except Exception:
            pass
    except Exception:
        pass

    return token

def get_expected_session_token() -> Optional[str]:
    """Get expected token from environment if configured."""
    token = os.environ.get("CLAY_SESSION_TOKEN", "").strip()
    return token if token else None

def verify_session_token(provided: str, expected: str) -> bool:
    """Constant-time token verification to mitigate timing attacks."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.strip(), expected.strip())

async def clay_auth_middleware(request: Request, call_next) -> Response:
    """Protect internal endpoints if CLAY_SESSION_TOKEN is set in environment."""
    expected_token = get_expected_session_token()
    if not expected_token:
        return await call_next(request)

    path = request.url.path
    if path in ("/health", "/healthz", "/", "/docs", "/openapi.json"):
        return await call_next(request)

    token = request.headers.get("X-Clay-Auth-Token", "").strip()
    if not token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not verify_session_token(token, expected_token):
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized: Missing or invalid authentication token"}
        )

    return await call_next(request)
