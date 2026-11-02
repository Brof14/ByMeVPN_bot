"""Create a read-only overlay of the legacy API with trial bootstrap routes."""
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "# BYMEVPN_LEGACY_TRIAL_BOOTSTRAP_V1"
HOOK = f"""

{MARKER}
from legacy_trial_bootstrap import install_legacy_trial_bootstrap
install_legacy_trial_bootstrap(api_router, globals())
"""


def patched_text(source: str) -> str:
    if MARKER in source:
        return source
    required = (
        "api_router = APIRouter()",
        '@api_router.post("/subscription/trial")',
        '@api_router.get("/servers")',
        '@api_router.post("/vpn/session")',
        "class TrialRequest(",
        "class SessionRequest(",
        "def _subscription_headers_from_request(",
        "def _entitlement_requested(",
        "def parse_single_node_line(",
    )
    missing = [item for item in required if item not in source]
    if missing:
        raise ValueError("Source is not a compatible legacy mobile API router")
    return source.rstrip() + HOOK


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.source.read_text(encoding="utf-8")
    rendered = patched_text(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite an existing overlay")
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
