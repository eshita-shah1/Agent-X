from fastapi import APIRouter

from app.api.v1 import account, auth, market

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(account.router)
api_router.include_router(market.router)
