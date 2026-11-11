from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user, get_db
from app.api.v1.schemas import AccountResponse
from app.db.models import User, VirtualAccount
from app.db.scoping import owned_select

router = APIRouter(prefix="/account", tags=["account"])


@router.get("", response_model=AccountResponse)
def get_account(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> VirtualAccount:
    # No user id is accepted from the client; ownership comes from the authenticated user.
    account = db.scalar(owned_select(VirtualAccount, user.id))
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account
