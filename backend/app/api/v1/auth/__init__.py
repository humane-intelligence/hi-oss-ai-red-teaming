"""Auth API surface — OIDC and simple (email + password) flows.

Flow-specific endpoints live in submodules; this module exposes the combined
router. The session endpoints that are independent of the login flow used to
obtain the bearer token live in `me.py`.
"""

from fastapi import APIRouter

from app.api.v1.auth.invitations import router as invitations_router
from app.api.v1.auth.login import router as login_router
from app.api.v1.auth.me import router as me_router
from app.api.v1.auth.oidc import router as oidc_router
from app.api.v1.auth.password_resets import router as password_resets_router
from app.api.v1.auth.refresh import router as refresh_router
from app.api.v1.auth.register import router as register_router
from app.api.v1.auth.users import router as users_router

router = APIRouter(prefix="/auth", tags=["auth"])
router.include_router(login_router)
router.include_router(refresh_router)
router.include_router(oidc_router)
router.include_router(users_router)
router.include_router(invitations_router)
router.include_router(register_router)
router.include_router(password_resets_router)
router.include_router(me_router)
