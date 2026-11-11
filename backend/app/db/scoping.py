"""Helpers that make user isolation the default for user-owned data.

User identity comes only from the authenticated request (see api/v1/deps.py), never from a
client-supplied field. Any query over a user-owned model must go through `owned_select`.
"""

import uuid
from typing import TypeVar

from sqlalchemy import Select, select

from app.db.base import UserOwnedMixin

M = TypeVar("M", bound=UserOwnedMixin)


def owned_select(model: type[M], user_id: uuid.UUID) -> Select[tuple[M]]:
    if not issubclass(model, UserOwnedMixin):
        raise TypeError(f"{model.__name__} is not a user-owned model")
    return select(model).where(model.user_id == user_id)
