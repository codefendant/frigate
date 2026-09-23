"""Auth apis."""

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt
from peewee import DoesNotExist
from slowapi import Limiter

from frigate.api.defs.request.app_body import (
    AppPostLoginBody,
    AppPostUsersBody,
    AppPutPasswordBody,
    AppPutRoleBody,
)
from frigate.api.defs.tags import Tags
from frigate.api.media_auth import (
    check_camera_access,
    deny_response_for_media_uri,
    is_role_restricted,
)
from frigate.config import AuthConfig, ProxyConfig
from frigate.const import CONFIG_DIR, JWT_SECRET_ENV_VAR, PASSWORD_HASH_ALGORITHM
from frigate.models import User
from frigate.notices import raise_notice

logger = logging.getLogger(__name__)

# In-memory cache to track which clients we've logged for an anonymous access event.
# Keyed by a hashed value combining remote address + user-agent. The value is
# an expiration timestamp (float).
FIRST_LOAD_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
_first_load_seen: dict[str, float] = {}


def require_admin_by_default():
    """
    Global admin requirement dependency for all endpoints by default.

    This is set as the default dependency on the FastAPI app to ensure all
    endpoints require admin access unless explicitly overridden with
    allow_public(), allow_any_authenticated(), or require_role().

    Internal port always has admin role set by the /auth endpoint,
    so this check passes automatically for internal requests.

    Certain paths are exempted from the global admin check because they must
    be accessible before authentication (login, auth) or they have their own
    route-level authorization dependencies that handle access control.
    """
    EXEMPT_PATHS = {
        "/auth",
        "/auth/first_time_login",
        "/login",
        "/logout",
        "/profile",
        "/profiles",
        "/",
        "/version",
        "/config/schema.json",
        "/metrics",
        "/stats",
        "/stats/history",
        "/config",
        "/vainfo",
        "/nvinfo",
        "/labels",
        "/sub_labels",
        "/categorized_object_names",
        "/plus/models",
        "/recognized_license_plates",
        "/classification/attributes",
        "/timeline",
        "/timeline/hourly",
        "/recordings/storage",
        "/recordings/summary",
        "/recordings/unavailable",
        "/go2rtc/streams",
        "/event_ids",
        "/events",
        "/cases",
        "/exports",
        "/jobs/export",
    }

    EXEMPT_PREFIXES = (
        "/logs/",
        "/review",
        "/reviews/",
        "/events/",
        "/export/",
        "/go2rtc/streams/",
        "/users/",
        "/preview/",
        "/cases/",
        "/exports/",
        "/jobs/export/",
        "/vod/",
        "/notifications/",
    )

    async def admin_checker(request: Request):
        path = request.url.path
        if path in EXEMPT_PATHS:
            return
        if path.startswith(EXEMPT_PREFIXES):
            return
        try:
            if path.startswith("/"):
                first_segment = path.split("/", 2)[1]
                if (
                    first_segment
                    and first_segment in request.app.frigate_config.cameras
                ):
                    return
        except Exception:
            pass
        role = request.headers.get("remote-role")
        if role == "admin":
            return
        raise HTTPException(
            status_code=403,
            detail="Access denied. A user with the admin role is required.",
        )

    return admin_checker


def _is_authenticated(request: Request) -> bool:
    username = request.headers.get("remote-user")
    return username is not None and username != "anonymous"


def allow_public():
    async def public_checker(request: Request):
        return

    return public_checker


def allow_any_authenticated():
    async def auth_checker(request: Request):
        username = request.headers.get("remote-user")
        role = request.headers.get("remote-role")
        if role != "admin":
            if username is None or not _is_authenticated(request):
                raise HTTPException(status_code=401, detail="Authentication required")
        return

    return auth_checker


router = APIRouter(tags=[Tags.auth])


@router.get("/auth/first_time_login", dependencies=[Depends(allow_public())])
def first_time_login(request: Request):
    auth_config = request.app.frigate_config.auth
    return JSONResponse(
        content={
            "admin_first_time_login": auth_config.admin_first_time_login
            or auth_config.reset_admin_password
        }
    )


class RateLimiter:
    _limit = ""

    def set_limit(self, limit: str):
        self._limit = limit

    def get_limit(self) -> str:
        return self._limit


rateLimiter = RateLimiter()
FAILED_LOGIN_BURST_GAP_S = 300
MAX_NOTICE_USERNAME = 64
MAX_OPEN_BURSTS = 100


class FailedLoginTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bursts: dict[str, tuple[int, float]] = {}

    def record(self, user: str, now: float, *, known: bool = False) -> None:
        user = user[:MAX_NOTICE_USERNAME]
        with self._lock:
            while self._bursts:
                stalest = next(iter(self._bursts))
                if now - self._bursts[stalest][1] < FAILED_LOGIN_BURST_GAP_S:
                    break
                del self._bursts[stalest]
            if (
                not known
                and user not in self._bursts
                and len(self._bursts) >= MAX_OPEN_BURSTS
            ):
                return
            start, _ = self._bursts.pop(user, (int(now), now))
            self._bursts[user] = (start, now)
        raise_notice("failed_login", scope=f"{user}:{start}", params={"user": user})


failed_logins = FailedLoginTracker()


def get_remote_addr(request: Request):
    direct_addr = request.client.host if request.client else None
    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return direct_addr or "127.0.0.1"
    route = list(reversed(forwarded_for.split(",")))
    logger.debug(f"IP Route: {[r for r in route]}")
    trusted_proxies = []
    for proxy in request.app.frigate_config.auth.trusted_proxies:
        try:
            network = ipaddress.ip_network(proxy)
        except ValueError:
            logger.warning(f"Unable to parse trusted network: {proxy}")
            continue
        trusted_proxies.append(network)
    for addr in route:
        ip = ipaddress.ip_address(addr.strip())
        trusted = False
        for trusted_proxy in trusted_proxies:
            if trusted_proxy.version == 4:
                ipv4 = ip.ipv4_mapped if ip.version == 6 else ip
                if ipv4 is not None and ipv4 in trusted_proxy:
                    trusted = True
                    break
            elif trusted_proxy.version == 6 and ip.version == 6:
                if ip in trusted_proxy:
                    trusted = True
                    break
        if trusted:
            continue
        return str(ip)
    return direct_addr or "127.0.0.1"


def _cleanup_first_load_seen() -> None:
    now = time.time()
    expired = [k for k, exp in _first_load_seen.items() if exp <= now]
    for k in expired:
        del _first_load_seen[k]


def get_jwt_secret() -> str:
    jwt_secret = None
    if JWT_SECRET_ENV_VAR in os.environ:
        logger.debug(
            f"Using jwt secret from {JWT_SECRET_ENV_VAR} environment variable."
        )
        jwt_secret = os.environ.get(JWT_SECRET_ENV_VAR)
    elif os.path.isfile(os.path.join("/run/secrets", JWT_SECRET_ENV_VAR)):
        logger.debug(f"Using jwt secret from {JWT_SECRET_ENV_VAR} docker secret file.")
        jwt_secret = (
            Path(os.path.join("/run/secrets", JWT_SECRET_ENV_VAR)).read_text().strip()
        )
    elif os.path.isfile("/data/options.json"):
        try:
            with open("/data/options.json") as f:
                raw_options = f.read()
            logger.debug("Using jwt secret from Home Assistant Add-on options file.")
            options = json.loads(raw_options)
            jwt_secret = options.get("jwt_secret")
        except (OSError, ValueError):
            logger.warning(
                "Unable to read Home Assistant add-on options; falling back to the Frigate JWT secret file."
            )

    if jwt_secret is None:
        jwt_secret_file = os.path.join(CONFIG_DIR, ".jwt_secret")
        if not os.path.isfile(jwt_secret_file):
            logger.debug(
                "No jwt secret found. Generating one and storing in .jwt_secret file in config directory."
            )
            jwt_secret = secrets.token_hex(64)
            try:
                fd = os.open(
                    jwt_secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(fd, "w") as f:
                    f.write(str(jwt_secret))
            except Exception:
                logger.warning(
                    "Unable to write jwt token file to config directory. A new jwt token will be created at each startup."
                )
        else:
            logger.debug("Using jwt secret from .jwt_secret file in config directory.")
            with open(jwt_secret_file) as f:
                try:
                    jwt_secret = f.readline().strip()
                except Exception:
                    logger.warning(
                        "Unable to read jwt token from .jwt_secret file in config directory. A new jwt token will be created at each startup."
                    )
                    jwt_secret = secrets.token_hex(64)

    if len(jwt_secret) < 64:
        logger.warning("JWT Secret is recommended to be 64 characters or more")

    return jwt_secret


def hash_password(password: str, salt=None, iterations=600000):
    if salt is None:
        salt = secrets.token_hex(16)
    assert salt and isinstance(salt, str) and "$" not in salt
    assert isinstance(password, str)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    b64_hash = base64.b64encode(pw_hash).decode("ascii").strip()
    return "{}${}${}${}".format(PASSWORD_HASH_ALGORITHM, iterations, salt, b64_hash)


def verify_password(password, password_hash):
    if (password_hash or "").count("$") != 3:
        return False
    algorithm, iterations, salt, b64_hash = password_hash.split("$", 3)
    iterations = int(iterations)
    assert algorithm == PASSWORD_HASH_ALGORITHM
    compare_hash = hash_password(password, salt, iterations)
    return secrets.compare_digest(password_hash, compare_hash)


def validate_password_strength(password: str) -> tuple[bool, str | None]:
    if len(password) < 12:
        return False, "Password must be at least 12 characters long"
    if len(password) > 128:
        return False, "Password cannot be longer than 128 characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number"
    if not re.search(r"[^A-Za-z0-9]", password):
        return False, "Password must contain at least one special character"
    return True, None


def get_user_by_username(username: str) -> User | None:
    try:
        return User.get(User.username == username)
    except DoesNotExist:
        return None


def create_access_token(data: dict, secret: str, expires_delta: float) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow().timestamp() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode({"alg": "HS256"}, to_encode, secret)


def decode_access_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret)


def get_allowed_origins(auth_config: AuthConfig) -> list[str]:
    return auth_config.allowed_origins


def validate_origin(origin: str, auth_config: AuthConfig) -> bool:
    if origin in auth_config.allowed_origins:
        return True
    parsed = urlparse(origin)
    if parsed.hostname in auth_config.allowed_origins:
        return True
    return False


def get_proxy_secret(proxy: ProxyConfig) -> str | None:
    return proxy.secret


def get_query_params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


@router.post("/login", dependencies=[Depends(allow_public())])
def login(request: Request):
    return RedirectResponse(url="/")


@router.post("/logout", dependencies=[Depends(allow_any_authenticated())])
def logout(request: Request):
    return JSONResponse(content={"success": True})


limiter = Limiter(key_func=get_remote_addr)
