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


if __name__ == "__main__":
    main()
