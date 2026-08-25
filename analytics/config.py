from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SANITIZED_DEFAULTS: Mapping[str, object] = {
    "daily_screening_target": 100,
    "daily_submission_soft_capacity": 20,
    "max_active_applications_per_company": 2,
    "stale_after_days": 14,
    "gmail_overlap_days": 7,
    "reporting_timezone": "UTC",
}
_GMAIL_SETUP_INSTRUCTION = (
    "Copy analytics/config.example.json to analytics/config.json and edit "
    "gmail_account_alias and gmail_expected_address before using --sync-gmail."
)
_EMAIL_ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PLACEHOLDER_ADDRESSES = frozenset(
    {"you@example.com", "you@example.test", "candidate@example.test"}
)
_PLACEHOLDER_ALIASES = frozenset({"change_me", "your-account-alias"})


class AnalyticsConfigError(ValueError):
    """A precise, payload-free local analytics configuration error."""


def _read_config(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalyticsConfigError(f"Cannot load local analytics config: {path}") from exc
    if not isinstance(value, dict):
        raise AnalyticsConfigError(f"Local analytics config must be a JSON object: {path}")
    return dict(value)


def load_local_config(
    path: Path,
    *,
    require_gmail: bool = False,
) -> dict[str, object]:
    local = _read_config(path)
    config = {**SANITIZED_DEFAULTS, **local}
    aliases = config.get("company_aliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in aliases.items()
    ):
        raise AnalyticsConfigError("company_aliases must map non-empty strings to strings")
    config["company_aliases"] = {
        key.strip().casefold(): value.strip().casefold() for key, value in aliases.items()
    }

    timezone_name = config.get("reporting_timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise AnalyticsConfigError("reporting_timezone must be a non-empty IANA timezone")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise AnalyticsConfigError(
            f"invalid reporting_timezone: {timezone_name!r}"
        ) from exc

    if not require_gmail:
        return config

    account_alias = local.get("gmail_account_alias")
    expected_address = local.get("gmail_expected_address")
    if (
        not isinstance(account_alias, str)
        or not account_alias.strip()
        or len(account_alias) > 128
        or account_alias.strip().casefold() in _PLACEHOLDER_ALIASES
        or not isinstance(expected_address, str)
        or not _EMAIL_ADDRESS.fullmatch(expected_address.strip())
        or expected_address.strip().casefold() in _PLACEHOLDER_ADDRESSES
    ):
        raise AnalyticsConfigError(_GMAIL_SETUP_INSTRUCTION)
    config["gmail_account_alias"] = account_alias.strip()
    config["gmail_expected_address"] = expected_address.strip()
    return config
