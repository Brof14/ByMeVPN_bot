"""Add JSON-body Telegram polling to a legacy GET-only FastAPI router."""

from typing import Any, Awaitable, Callable, Type

from fastapi import APIRouter, Request
from pydantic import BaseModel


class TelegramSessionTokenBody(BaseModel):
    token: str


LegacyTelegramCheck = Callable[[str, Request], Awaitable[Any]]


def register_telegram_check_post(
    router: APIRouter,
    legacy_check: LegacyTelegramCheck,
    response_model: Type[BaseModel],
) -> Callable[[TelegramSessionTokenBody, Request], Awaitable[Any]]:
    """Register POST body polling by delegating to the existing GET handler.

    This keeps Telegram state validation, authentication, and consumption in
    one implementation while preserving the old GET route unchanged.
    """
    path = "/auth/telegram/check-session"
    if any(path == route.path and "POST" in (route.methods or set()) for route in router.routes):
        raise RuntimeError("Telegram check-session POST route is already registered")

    async def check_session_body(body: TelegramSessionTokenBody, request: Request):
        if not body.token or len(body.token) > 256:
            return response_model(status="expired")
        return await legacy_check(body.token, request)

    router.add_api_route(
        path,
        check_session_body,
        methods=["POST"],
        response_model=response_model,
        name="check_telegram_session_body",
    )
    return check_session_body
