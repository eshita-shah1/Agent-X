import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db, get_settings_dep
from app.api.v1.schemas import (
    AccountResponse,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from app.core.config import Settings
from app.core.security import create_access_token, hash_password, password_needs_rehash, verify_password
from app.db.models import AuditEvent, User, VirtualAccount

router = APIRouter(prefix="/auth", tags=["auth"])

# Agent-X targets the Indian NSE market initially, so accounts are denominated in INR.
ACCOUNT_CURRENCY = "INR"


def _client_ip(request: Request) -> str | None:
    """The peer address if it is a real IP; None otherwise (unix sockets, test clients)."""
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> RegisterResponse:
    user = User(email=body.email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered") from None
    account = VirtualAccount(user_id=user.id, currency=ACCOUNT_CURRENCY, initial_capital=body.initial_capital)
    db.add(account)
    db.add(AuditEvent(user_id=user.id, event_type="user.registered", client_ip=_client_ip(request)))
    db.commit()
    db.refresh(account)  # return the stored (quantised) values, not the request's
    return RegisterResponse(
        user=UserResponse.model_validate(user), account=AccountResponse.model_validate(account)
    )


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == body.email))
    # Always run a verification (against a dummy hash if the user is unknown) to equalise timing.
    valid = verify_password(body.password, user.password_hash if user else None)
    if not valid or user is None or not user.is_active:
        db.add(
            AuditEvent(
                user_id=user.id if user else None,
                event_type="auth.login_failed",
                details={"email": body.email},
                client_ip=_client_ip(request),
            )
        )
        db.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid email or password", headers={"WWW-Authenticate": "Bearer"}
        )
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    db.add(AuditEvent(user_id=user.id, event_type="auth.login_succeeded", client_ip=_client_ip(request)))
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, settings),
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> User:
    return user
