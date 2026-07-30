"""Authentication routes for user login, registration, and session management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, Request
from pydantic import BaseModel, EmailStr, Field

from core.auth_cookies import get_session_token, session_cookie_names
from core.container import container
from core.config import Settings
from core.logging import get_logger
from core.rate_limit import SlidingWindowLimiter
from services.user_auth import UserAuthService
from services.auth import AuthService

logger = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Module-level so the window survives across requests. Built lazily from
# settings on first use -- `container.settings()` is not resolvable at import.
_login_limiter: Optional[SlidingWindowLimiter] = None
_register_limiter: Optional[SlidingWindowLimiter] = None


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limiters(settings: Settings) -> tuple[SlidingWindowLimiter, SlidingWindowLimiter]:
    global _login_limiter, _register_limiter
    if _login_limiter is None or _register_limiter is None:
        attempts = settings.auth_rate_limit_attempts
        window = settings.auth_rate_limit_window
        _login_limiter = SlidingWindowLimiter(max_events=attempts, window_seconds=window)
        _register_limiter = SlidingWindowLimiter(max_events=attempts, window_seconds=window)
    return _login_limiter, _register_limiter


def _enforce_limit(limiter: SlidingWindowLimiter, key: str, settings: Settings) -> None:
    if not settings.auth_rate_limit_enabled:
        return
    if not limiter.hit(key):
        retry_after = limiter.retry_after(key)
        logger.warning("Auth rate limit tripped for key=%s", key)
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please wait and try again.",
            headers={"Retry-After": str(retry_after)},
        )


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    # Mirrors the max_length=100 column on User.display_name so the rejection
    # is a 422 at the edge rather than a silent SQLite truncation.
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    is_owner: bool


def get_user_auth_service() -> UserAuthService:
    return container.user_auth_service()


def get_settings() -> Settings:
    return container.settings()


def get_auth_service() -> AuthService:
    return container.auth_service()


@router.get("/status")
async def get_auth_status(
    request: Request, user_auth: UserAuthService = Depends(get_user_auth_service), settings: Settings = Depends(get_settings)
):
    """
    Get authentication status.
    Returns auth mode and current user if authenticated.
    """
    status = user_auth.get_auth_status()

    # Check if user has a valid session
    token = get_session_token(request.cookies, settings)
    current_user = None

    if token:
        user = await user_auth.get_current_user(token)
        if user:
            current_user = {"id": user.id, "email": user.email, "display_name": user.display_name, "is_owner": user.is_owner}

    # Check if registration is available
    can_register = await user_auth.can_register()

    # Determine if auth is enabled from server config
    auth_enabled = True
    if settings.vite_auth_enabled and settings.vite_auth_enabled.lower() == "false":
        auth_enabled = False

    return {
        "auth_enabled": auth_enabled,
        "auth_mode": status["auth_mode"],
        "authenticated": current_user is not None,
        "user": current_user,
        "can_register": can_register,
    }


@router.post("/register")
async def register(
    request: RegisterRequest,
    response: Response,
    http_request: Request,
    user_auth: UserAuthService = Depends(get_user_auth_service),
    settings: Settings = Depends(get_settings),
):
    """
    Register a new user.
    In single-owner mode, only the first user can register.
    In multi-user mode, anyone can register.
    """
    _, register_limiter = _limiters(settings)
    _enforce_limit(register_limiter, _client_key(http_request), settings)

    user, error = await user_auth.register(email=request.email, password=request.password, display_name=request.display_name)

    if error:
        raise HTTPException(status_code=400, detail=error)

    # Create token and set cookie
    token = user_auth.create_access_token(user)
    response.set_cookie(
        key=settings.jwt_cookie_name,
        value=token,
        httponly=True,
        secure=settings.jwt_cookie_secure,
        samesite=settings.jwt_cookie_samesite,
        max_age=settings.jwt_expire_minutes * 60,
    )

    return {"success": True, "user": {"id": user.id, "email": user.email, "display_name": user.display_name, "is_owner": user.is_owner}}


@router.post("/login")
async def login(
    request: LoginRequest,
    response: Response,
    http_request: Request,
    user_auth: UserAuthService = Depends(get_user_auth_service),
    settings: Settings = Depends(get_settings),
):
    """
    Login with email and password.
    Sets HttpOnly cookie with JWT token.
    """
    # Keyed on (source address, email) so one attacker cannot lock out an
    # unrelated account from a shared NAT, and a password-spray against many
    # accounts from one address is still bounded per account.
    login_limiter, _ = _limiters(settings)
    limit_key = f"{_client_key(http_request)}|{request.email.lower()}"
    _enforce_limit(login_limiter, limit_key, settings)

    user, error = await user_auth.login(email=request.email, password=request.password)

    if error:
        raise HTTPException(status_code=401, detail=error)

    # Clear the counter so a user who eventually gets it right is not still
    # throttled by their own earlier typos.
    login_limiter.reset(limit_key)

    # Create token and set cookie
    token = user_auth.create_access_token(user)
    response.set_cookie(
        key=settings.jwt_cookie_name,
        value=token,
        httponly=True,
        secure=settings.jwt_cookie_secure,
        samesite=settings.jwt_cookie_samesite,
        max_age=settings.jwt_expire_minutes * 60,
    )

    return {"success": True, "user": {"id": user.id, "email": user.email, "display_name": user.display_name, "is_owner": user.is_owner}}


@router.post("/logout")
async def logout(
    response: Response,
    user_auth: UserAuthService = Depends(get_user_auth_service),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
):
    """
    Logout by clearing the session cookie and the API-key memory cache.

    What this actually does:
    - Deletes the session cookie (canonical + legacy names)
    - Clears the decrypted API-key cache

    What it does NOT do, despite what this docstring used to claim:
    - It does not clear any encryption key. `user_auth.logout()` is a no-op
      log line, and the encryption key is server-scoped -- initialised once
      in `main.py` from `API_KEY_ENCRYPTION_KEY`, never per session.
    - It does not invalidate the JWT. There is no denylist, so a token
      captured before logout stays valid until `exp`. The only revocation
      lever is `User.is_active`, enforced in `get_current_user`.
    See the Known Limitations section of docs-internal/authentication.md.
    """
    user_auth.logout()

    # Clear API key memory cache
    auth_service.clear_cache()

    # Delete auth cookie
    for cookie_name in session_cookie_names(settings):
        response.delete_cookie(
            key=cookie_name,
            httponly=True,
            secure=settings.jwt_cookie_secure,
            samesite=settings.jwt_cookie_samesite,
        )
    return {"success": True}


@router.get("/me")
async def get_current_user(
    request: Request, user_auth: UserAuthService = Depends(get_user_auth_service), settings: Settings = Depends(get_settings)
):
    """
    Get current authenticated user.
    Requires valid session cookie.
    """
    token = get_session_token(request.cookies, settings)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await user_auth.get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return {"id": user.id, "email": user.email, "display_name": user.display_name, "is_owner": user.is_owner}
