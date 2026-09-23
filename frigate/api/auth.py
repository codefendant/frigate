import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Annotated, Any, Union

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from pydantic import BaseModel

from frigate.config import FrigateConfig
from frigate.const import CONFIG_DIR, JWT_SECRET_ENV_VAR

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

FIRST_LOAD_TTL = 30
_first_load_seen: dict[str, float] = {}


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: str | None = None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def _cleanup_first_load_seen() -> None:
    """Cleanup expired entries in the in-memory first-load cache."""
    now = time.time()
    # Build list for removal to avoid mutating dict during iteration
    expired = [k for k, exp in _first_load_seen.items() if exp <= now]
    for k in expired:
        del _first_load_seen[k]


def get_jwt_secret() -> str:
    jwt_secret = None
    # check env var
    if JWT_SECRET_ENV_VAR in os.environ:
        logger.debug(
            f"Using jwt secret from {JWT_SECRET_ENV_VAR} environment variable."
        )
        jwt_secret = os.environ.get(JWT_SECRET_ENV_VAR)
    # check docker secrets
    elif os.path.isfile(os.path.join("/run/secrets", JWT_SECRET_ENV_VAR)):
        logger.debug(f"Using jwt secret from {JWT_SECRET_ENV_VAR} docker secret file.")
        jwt_secret = (
            Path(os.path.join("/run/secrets", JWT_SECRET_ENV_VAR)).read_text().strip()
        )
    # check for the add-on options file
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
        # check .jwt_secrets file
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
                jwt_secret = f.read().strip()

    return jwt_secret


def create_access_token(data: dict, expires_delta: float) -> str:
    to_encode = data.copy()
    expire = time.time() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, get_jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
):
    config: FrigateConfig = request.app.frigate_config
    if not config.auth.enabled:
        return None

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    username: str | None = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenData(username=username)


async def require_authenticated_user(
    current_user: Annotated[TokenData | None, Depends(get_current_user)],
):
    return current_user


def get_remote_addr(request: Request) -> str:
    if "x-forwarded-for" in request.headers:
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else ""


def _get_auth_config(request: Request):
    return request.app.frigate_config.auth


def _get_cookie_name(request: Request) -> str:
    return _get_auth_config(request).cookie_name


def _get_cookie_secure(request: Request) -> bool:
    return _get_auth_config(request).cookie_secure


def _get_cookie_samesite(request: Request):
    return _get_auth_config(request).cookie_samesite


def _get_cookie_domain(request: Request):
    return _get_auth_config(request).cookie_domain


def _get_cookie_path(request: Request):
    return _get_auth_config(request).cookie_path


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_auth_cookie(response, token: str, request: Request):
    response.set_cookie(
        _get_cookie_name(request),
        token,
        httponly=True,
        secure=_get_cookie_secure(request),
        samesite=_get_cookie_samesite(request),
        domain=_get_cookie_domain(request),
        path=_get_cookie_path(request),
    )


def clear_auth_cookie(response, request: Request):
    response.delete_cookie(
        _get_cookie_name(request),
        secure=_get_cookie_secure(request),
        samesite=_get_cookie_samesite(request),
        domain=_get_cookie_domain(request),
        path=_get_cookie_path(request),
    )


def generate_api_key(secret: str) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def validate_api_key(api_key: str, secret: str) -> bool:
    return hmac.compare_digest(api_key, secret)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(api_key: str, hashed_api_key: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key), hashed_api_key)
