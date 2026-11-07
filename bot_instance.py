"""Runtime mode helpers for running a secondary polling bot safely."""

PRIMARY = "primary"
POLLING_ONLY = "polling-only"
SUPPORTED_MODES = {PRIMARY, POLLING_ONLY}


def normalize_bot_instance_mode(value: str | None) -> str:
    mode = (value or PRIMARY).strip().lower()
    if mode not in SUPPORTED_MODES:
        choices = ", ".join(sorted(SUPPORTED_MODES))
        raise ValueError(f"BOT_INSTANCE_MODE must be one of: {choices}")
    return mode


def runs_primary_services(mode: str) -> bool:
    return normalize_bot_instance_mode(mode) == PRIMARY
