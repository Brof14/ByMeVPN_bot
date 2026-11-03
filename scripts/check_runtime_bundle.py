"""Fail image builds that omit modules or storage helpers required at runtime."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DATABASE_FUNCTIONS = {
    "create_mobile_google_nonce",
    "consume_mobile_google_nonce",
    "get_or_create_google_account",
    "create_mobile_trial_bootstrap",
    "get_mobile_trial_bootstrap",
    "delete_mobile_trial_bootstrap",
    "create_mobile_auth_session",
    "confirm_mobile_auth_session",
    "poll_mobile_auth_session",
    "get_mobile_auth_session",
    "consume_mobile_auth_session",
    "cancel_mobile_auth_session",
}


def main() -> None:
    compatibility_module = ROOT / "telegram_post_compat.py"
    if not compatibility_module.is_file():
        raise SystemExit("telegram_post_compat.py missing from image")

    database_path = ROOT / "database.py"
    tree = ast.parse(database_path.read_text(encoding="utf-8"))
    functions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = sorted(REQUIRED_DATABASE_FUNCTIONS - functions)
    if missing:
        raise SystemExit(
            "database.py missing mobile compatibility functions: " + ",".join(missing)
        )

    start_path = ROOT / "handlers" / "start.py"
    start_tree = ast.parse(start_path.read_text(encoding="utf-8"))
    start_handler = next(
        (node for node in start_tree.body
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "cmd_start"),
        None,
    )
    if start_handler is None:
        raise SystemExit("Telegram /start handler missing from image")
    confirms_session_awaited = any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "confirm_telegram_session"
        for node in ast.walk(start_handler)
    )
    if not confirms_session_awaited:
        raise SystemExit("Telegram /start handler must await session confirmation")
    try:
        from jwt.api_jwk import PyJWKSet  # noqa: F401
        from jwt.exceptions import PyJWKClientError  # noqa: F401
    except ImportError as exc:
        raise SystemExit("PyJWT JWK support is required for Google's published signing keys") from exc


if __name__ == "__main__":
    main()
