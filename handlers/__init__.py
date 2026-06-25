from .start import router as start_router
from .buy import router as buy_router
from .keys import router as keys_router
from .partner import router as partner_router
from .guide import router as guide_router
from .legal import router as legal_router
from .admin import router as admin_router
from .auth import router as auth_router
from .fallback import router as fallback_router

__all__ = [
    "start_router",
    "buy_router",
    "keys_router",
    "partner_router",
    "guide_router",
    "legal_router",
    "admin_router",
    "auth_router",
    "fallback_router",
]
