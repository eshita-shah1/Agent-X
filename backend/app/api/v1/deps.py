from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import InvalidTokenError, decode_access_claims
from app.db.models import User
from app.db.session import session_scope

_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    yield from session_scope(request.app.state.sessionmaker)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    """The only source of user identity for protected endpoints."""
    if credentials is None:
        raise _UNAUTHORIZED
    try:
        user_id, issued_at = decode_access_claims(credentials.credentials, settings)
    except InvalidTokenError:
        raise _UNAUTHORIZED from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    # Logout (or an operator) revoked every token issued up to this instant (migration 0014). JWT iat has 1 s
    # resolution, so a token issued in the same second as the revocation is refused too.
    if user.sessions_revoked_at is not None and issued_at <= user.sessions_revoked_at:
        raise _UNAUTHORIZED
    request.state.user_id = str(user.id)  # for the request log (app.core.http)
    return user
