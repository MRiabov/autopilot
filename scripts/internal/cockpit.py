from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    CockpitCodexAccount,
    CockpitCodexQuota,
    CockpitCodexStoreSnapshot,
    CockpitCodexSwitchCandidate,
    CockpitCodexSwitchSettings,
    CockpitCodexTokens,
)
from .utils import read_text, to_jsonable, write_text


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def normalize_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def clamp_percentage(value: Any, default: int = 0) -> int:
    normalized = normalize_int(value)
    if normalized is None:
        return default
    return max(0, min(100, normalized))


def format_cockpit_quota_metric_label(minutes: int | None, fallback: str) -> str:
    if minutes is None or minutes <= 0:
        return fallback

    weeks_minutes = 7 * 24 * 60
    day_minutes = 24 * 60
    hour_minutes = 60

    if minutes >= weeks_minutes - 1:
        weeks = (minutes + weeks_minutes - 1) // weeks_minutes
        return f"{weeks}w"
    if minutes >= day_minutes - 1:
        days = (minutes + day_minutes - 1) // day_minutes
        return f"{days}d"
    if minutes >= hour_minutes:
        hours = (minutes + hour_minutes - 1) // hour_minutes
        return f"{hours}h"
    return f"{minutes}m"


def parse_cockpit_codex_quota(raw: Any) -> CockpitCodexQuota | None:
    if not isinstance(raw, dict):
        return None
    return CockpitCodexQuota(
        hourly_percentage=clamp_percentage(raw.get("hourly_percentage")),
        weekly_percentage=clamp_percentage(raw.get("weekly_percentage")),
        hourly_window_minutes=normalize_int(raw.get("hourly_window_minutes")),
        weekly_window_minutes=normalize_int(raw.get("weekly_window_minutes")),
        hourly_window_present=normalize_bool(raw.get("hourly_window_present")),
        weekly_window_present=normalize_bool(raw.get("weekly_window_present")),
    )


def parse_cockpit_codex_tokens(raw: Any) -> CockpitCodexTokens | None:
    if not isinstance(raw, dict):
        return None
    id_token = normalize_text(raw.get("id_token"))
    access_token = normalize_text(raw.get("access_token"))
    if not id_token or not access_token:
        return None
    return CockpitCodexTokens(
        id_token=id_token,
        access_token=access_token,
        refresh_token=normalize_text(raw.get("refresh_token")),
        account_id=normalize_text(raw.get("account_id") or raw.get("accountId")),
    )


def parse_cockpit_codex_account(raw: Any) -> CockpitCodexAccount | None:
    if not isinstance(raw, dict):
        return None

    account_id = normalize_text(raw.get("id"))
    email = normalize_text(raw.get("email"))
    if not account_id or not email:
        return None

    auth_mode = normalize_text(raw.get("auth_mode") or raw.get("authMode")) or (
        "apikey" if normalize_text(raw.get("openai_api_key")) else "oauth"
    )

    tokens = parse_cockpit_codex_tokens(raw.get("tokens"))
    quota = parse_cockpit_codex_quota(raw.get("quota"))

    return CockpitCodexAccount(
        id=account_id,
        email=email,
        auth_mode=auth_mode.lower(),
        openai_api_key=normalize_text(raw.get("openai_api_key")),
        api_base_url=normalize_text(raw.get("api_base_url") or raw.get("apiBaseUrl")),
        account_id=normalize_text(raw.get("account_id") or raw.get("accountId")),
        organization_id=normalize_text(
            raw.get("organization_id") or raw.get("organizationId")
        ),
        plan_type=normalize_text(raw.get("plan_type") or raw.get("planType")),
        quota=quota,
        tokens=tokens,
        created_at=normalize_int(raw.get("created_at") or raw.get("createdAt")) or 0,
        last_used=normalize_int(raw.get("last_used") or raw.get("lastUsed")) or 0,
    )


def cockpit_data_dir_candidates(configured_data_dir: str = "") -> list[Path]:
    candidates: list[Path] = []
    if configured_data_dir.strip():
        candidates.append(Path(configured_data_dir).expanduser())

    env_override = os.environ.get("AUTOPILOT_COCKPIT_DATA_DIR") or os.environ.get(
        "COCKPIT_TOOLS_DATA_DIR"
    )
    if env_override:
        candidates.append(Path(env_override).expanduser())

    if sys.platform == "darwin":
        candidates.append(
            Path.home()
            / "Library"
            / "Application Support"
            / "com.antigravity.cockpit-tools"
        )
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "com.antigravity.cockpit-tools")
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        if xdg_data_home:
            candidates.append(
                Path(xdg_data_home).expanduser() / "com.antigravity.cockpit-tools"
            )
        candidates.append(
            Path.home() / ".local" / "share" / "com.antigravity.cockpit-tools"
        )

    candidates.append(Path.home() / ".antigravity_cockpit")
    seen: set[str] = set()
    unique_candidates: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)
    return unique_candidates


def load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        text = read_text(path).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_cockpit_codex_store(
    configured_data_dir: str = "",
) -> CockpitCodexStoreSnapshot | None:
    for data_dir in cockpit_data_dir_candidates(configured_data_dir):
        index_path = data_dir / "codex_accounts.json"
        accounts_dir = data_dir / "codex_accounts"
        if not index_path.exists() and not accounts_dir.exists():
            continue

        index_payload = load_json_object(index_path)
        current_account_id = (
            normalize_text((index_payload or {}).get("current_account_id"))
            if index_payload
            else None
        )
        auth_payload = load_json_object(data_dir / "auth.json")

        account_ids: list[str] = []
        if index_payload:
            raw_accounts = index_payload.get("accounts")
            if isinstance(raw_accounts, list):
                for item in raw_accounts:
                    if isinstance(item, dict):
                        account_id = normalize_text(item.get("id"))
                        if account_id:
                            account_ids.append(account_id)
        if accounts_dir.exists():
            for file_path in accounts_dir.glob("*.json"):
                account_ids.append(file_path.stem)

        deduped_ids: list[str] = []
        seen_ids: set[str] = set()
        for account_id in account_ids:
            if account_id in seen_ids:
                continue
            seen_ids.add(account_id)
            deduped_ids.append(account_id)

        accounts: list[CockpitCodexAccount] = []
        for account_id in deduped_ids:
            account_path = accounts_dir / f"{account_id}.json"
            raw_account = load_json_object(account_path)
            account = parse_cockpit_codex_account(raw_account) if raw_account else None
            if account:
                accounts.append(account)

        if accounts or index_payload or auth_payload:
            return CockpitCodexStoreSnapshot(
                data_dir=data_dir,
                index_path=index_path,
                accounts_dir=accounts_dir,
                current_account_id=current_account_id,
                index_payload=index_payload,
                auth_payload=auth_payload,
                accounts=accounts,
            )

    return None


def resolve_cockpit_current_account(
    store: CockpitCodexStoreSnapshot,
) -> CockpitCodexAccount | None:
    if store.current_account_id:
        for account in store.accounts:
            if account.id == store.current_account_id:
                return account

    auth_payload = store.auth_payload or {}
    auth_mode = normalize_text(auth_payload.get("auth_mode"))
    api_key = normalize_text(auth_payload.get("OPENAI_API_KEY"))

    if auth_mode == "apikey" or api_key:
        for account in store.accounts:
            if (
                account.is_api_key_auth()
                and normalize_text(account.openai_api_key) == api_key
            ):
                return account

    tokens = auth_payload.get("tokens")
    if isinstance(tokens, dict):
        auth_account_id = normalize_text(
            tokens.get("account_id") or tokens.get("accountId")
        )
        auth_org_id = normalize_text(
            tokens.get("organization_id") or tokens.get("organizationId")
        )
        if auth_account_id:
            for account in store.accounts:
                if normalize_text(account.account_id) == auth_account_id and (
                    not auth_org_id
                    or normalize_text(account.organization_id) == auth_org_id
                ):
                    return account
        if auth_org_id:
            for account in store.accounts:
                if normalize_text(account.organization_id) == auth_org_id:
                    return account

    if store.accounts:
        return max(store.accounts, key=lambda account: account.last_used)
    return None


def extract_cockpit_quota_metrics(
    account: CockpitCodexAccount,
) -> list[tuple[str, str, int]]:
    quota = account.quota
    if quota is None:
        return []

    has_presence = (
        quota.hourly_window_present is not None
        or quota.weekly_window_present is not None
    )
    metrics: list[tuple[str, str, int]] = []

    if not has_presence or quota.hourly_window_present:
        metrics.append(
            (
                "primary_window",
                format_cockpit_quota_metric_label(quota.hourly_window_minutes, "5h"),
                clamp_percentage(quota.hourly_percentage),
            )
        )

    if not has_presence or quota.weekly_window_present:
        metrics.append(
            (
                "secondary_window",
                format_cockpit_quota_metric_label(
                    quota.weekly_window_minutes, "Weekly"
                ),
                clamp_percentage(quota.weekly_percentage),
            )
        )

    if not metrics:
        metrics.append(
            (
                "primary_window",
                format_cockpit_quota_metric_label(quota.hourly_window_minutes, "5h"),
                clamp_percentage(quota.hourly_percentage),
            )
        )

    return metrics


def metric_crossed_threshold(
    metric: tuple[str, str, int], primary_threshold: int, secondary_threshold: int
) -> bool:
    key, _label, percentage = metric
    if key == "primary_window":
        return percentage <= primary_threshold
    if key == "secondary_window":
        return percentage <= secondary_threshold
    return False


def metric_above_threshold(
    metric: tuple[str, str, int], primary_threshold: int, secondary_threshold: int
) -> bool:
    key, _label, percentage = metric
    if key == "primary_window":
        return percentage > primary_threshold
    if key == "secondary_window":
        return percentage > secondary_threshold
    return True


def metric_margin_over_threshold(
    metric: tuple[str, str, int], primary_threshold: int, secondary_threshold: int
) -> int | None:
    key, _label, percentage = metric
    if key == "primary_window":
        return percentage - primary_threshold
    if key == "secondary_window":
        return percentage - secondary_threshold
    return None


def build_cockpit_switch_candidate(
    account: CockpitCodexAccount,
    primary_threshold: int,
    secondary_threshold: int,
) -> CockpitCodexSwitchCandidate | None:
    if not account.is_switchable():
        return None

    metrics = extract_cockpit_quota_metrics(account)
    if not metrics:
        return None
    if not all(
        metric_above_threshold(metric, primary_threshold, secondary_threshold)
        for metric in metrics
    ):
        return None

    margins = [
        margin
        for metric in metrics
        if (
            margin := metric_margin_over_threshold(
                metric, primary_threshold, secondary_threshold
            )
        )
        is not None
    ]
    if not margins:
        return None

    return CockpitCodexSwitchCandidate(
        account=account,
        min_margin=min(margins),
        min_percentage=min(metric[2] for metric in metrics),
        average_percentage=sum(metric[2] for metric in metrics) / len(metrics),
    )


def pick_best_cockpit_switch_candidate(
    candidates: list[CockpitCodexSwitchCandidate],
) -> CockpitCodexAccount | None:
    if not candidates:
        return None

    best = sorted(
        candidates,
        key=lambda candidate: (
            -candidate.min_margin,
            -candidate.min_percentage,
            -candidate.average_percentage,
            candidate.account.last_used,
        ),
    )[0]
    return best.account


def build_cockpit_auth_file_value(account: CockpitCodexAccount) -> dict[str, Any]:
    if account.is_api_key_auth():
        api_key = normalize_text(account.openai_api_key)
        if not api_key:
            raise ValueError("API key account is missing OPENAI_API_KEY")
        return {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": api_key,
        }

    if (
        not account.tokens
        or not normalize_text(account.tokens.id_token)
        or not normalize_text(account.tokens.access_token)
    ):
        raise ValueError("OAuth account is missing tokens")

    return {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": account.tokens.id_token,
            "access_token": account.tokens.access_token,
            "refresh_token": account.tokens.refresh_token,
            "account_id": account.account_id,
        },
        "last_refresh": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def normalize_api_base_url(raw: Any) -> str | None:
    text = normalize_text(raw)
    if not text:
        return None
    return text.rstrip("/")


def write_cockpit_config_toml(base_dir: Path, api_base_url: str | None) -> None:
    config_path = base_dir / "config.toml"
    normalized = normalize_api_base_url(api_base_url)

    if not config_path.exists() and normalized is None:
        return

    existing = read_text(config_path)
    lines = existing.splitlines()
    updated_lines: list[str] = []
    found = False
    key_pattern = re.compile(r"^\s*openai_base_url\s*=")

    for line in lines:
        if key_pattern.match(line):
            found = True
            if normalized is not None:
                updated_lines.append(f"openai_base_url = {json.dumps(normalized)}")
            continue
        updated_lines.append(line)

    if normalized is not None and not found:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append(f"openai_base_url = {json.dumps(normalized)}")

    write_text(
        config_path,
        "\n".join(updated_lines).rstrip("\n") + ("\n" if updated_lines else ""),
    )


def build_cockpit_keychain_account(base_dir: Path) -> str:
    resolved_home = base_dir.resolve(strict=False)
    digest_hex = hashlib.sha256(str(resolved_home).encode("utf-8")).hexdigest()
    return f"cli|{digest_hex[:16]}"


def write_cockpit_keychain_entry(base_dir: Path, account: CockpitCodexAccount) -> None:
    if sys.platform != "darwin" or account.is_api_key_auth():
        return

    payload = build_cockpit_auth_file_value(account)
    secret = json.dumps(payload, separators=(",", ":"))
    keychain_account = build_cockpit_keychain_account(base_dir)
    output = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            "Codex Auth",
            "-a",
            keychain_account,
            "-w",
            secret,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if output.returncode != 0:
        stderr = (output.stderr or "").strip()
        stdout = (output.stdout or "").strip()
        raise RuntimeError(
            "Failed to write Codex keychain entry: "
            + (stderr or stdout or f"status={output.returncode}")
        )


def write_cockpit_auth_file(base_dir: Path, account: CockpitCodexAccount) -> None:
    auth_path = base_dir / "auth.json"
    write_text(
        auth_path, json.dumps(build_cockpit_auth_file_value(account), indent=2) + "\n"
    )
    write_cockpit_config_toml(
        base_dir, account.api_base_url if account.is_api_key_auth() else None
    )
    write_cockpit_keychain_entry(base_dir, account)


def update_cockpit_account_index(
    store: CockpitCodexStoreSnapshot, account: CockpitCodexAccount
) -> None:
    payload = dict(store.index_payload or {})
    version = normalize_text(payload.get("version")) or "1.0"

    raw_accounts = payload.get("accounts")
    ordered_ids: list[str] = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if isinstance(item, dict):
                account_id = normalize_text(item.get("id"))
                if account_id:
                    ordered_ids.append(account_id)

    if account.id not in ordered_ids:
        ordered_ids.append(account.id)

    account_map = {stored.id: stored for stored in store.accounts}
    account_map[account.id] = account

    payload["version"] = version
    payload["current_account_id"] = account.id
    payload["accounts"] = [
        {
            "id": stored.id,
            "email": stored.email,
            "plan_type": stored.plan_type,
            "created_at": stored.created_at,
            "last_used": stored.last_used,
        }
        for stored in (
            account_map[account_id]
            for account_id in ordered_ids
            if account_id in account_map
        )
    ]

    write_text(store.index_path, json.dumps(payload, indent=2) + "\n")


def save_cockpit_account_record(
    store: CockpitCodexStoreSnapshot, account: CockpitCodexAccount
) -> None:
    account_path = store.accounts_dir / f"{account.id}.json"
    write_text(account_path, json.dumps(to_jsonable(account), indent=2) + "\n")


class CockpitCodexSwitcher:
    def __init__(
        self, log: Callable[[str], None], settings: CockpitCodexSwitchSettings
    ) -> None:
        self.log = log
        self.settings = settings

    def is_enabled(self, store: CockpitCodexStoreSnapshot | None = None) -> bool:
        mode = (self.settings.mode or "auto").strip().lower()
        if mode == "off":
            return False
        if mode == "on":
            return True
        if store is None:
            store = load_cockpit_codex_store()
        return bool(store and len(store.accounts) > 1)

    def load_store(
        self, configured_data_dir: str = ""
    ) -> CockpitCodexStoreSnapshot | None:
        return load_cockpit_codex_store(configured_data_dir)

    def current_account(
        self, store: CockpitCodexStoreSnapshot
    ) -> CockpitCodexAccount | None:
        return resolve_cockpit_current_account(store)

    def pick_target(
        self, store: CockpitCodexStoreSnapshot
    ) -> CockpitCodexAccount | None:
        current = self.current_account(store)
        if current is None:
            return None

        current_metrics = extract_cockpit_quota_metrics(current)
        if not current_metrics:
            return None

        should_switch = any(
            metric_crossed_threshold(
                metric,
                self.settings.primary_threshold,
                self.settings.secondary_threshold,
            )
            for metric in current_metrics
        )
        if not should_switch:
            return None

        candidates = [
            candidate
            for account in store.accounts
            if account.id != current.id
            if (
                candidate := build_cockpit_switch_candidate(
                    account,
                    self.settings.primary_threshold,
                    self.settings.secondary_threshold,
                )
            )
        ]
        return pick_best_cockpit_switch_candidate(candidates)

    def switch_to(
        self, store: CockpitCodexStoreSnapshot, account: CockpitCodexAccount
    ) -> CockpitCodexAccount:
        updated_account = replace(account, last_used=int(time.time()))
        self.log(
            f"[Codex switch] switching accounts: account_id={updated_account.id}, email={updated_account.email}, data_dir={store.data_dir}"
        )
        store.accounts_dir.mkdir(parents=True, exist_ok=True)
        write_cockpit_auth_file(store.data_dir, updated_account)
        save_cockpit_account_record(store, updated_account)
        update_cockpit_account_index(store, updated_account)
        self.log(f"[Codex switch] switched to: {updated_account.email}")
        return updated_account

    def maybe_switch(self, configured_data_dir: str = "") -> CockpitCodexAccount | None:
        mode = (self.settings.mode or "auto").strip().lower()
        if mode == "off":
            return None
        store = self.load_store(configured_data_dir)
        if store is None or not store.accounts:
            return None
        if mode == "auto" and len(store.accounts) <= 1:
            return None

        target = self.pick_target(store)
        if target is None:
            return None

        try:
            return self.switch_to(store, target)
        except Exception as exc:
            self.log(f"[Codex switch] failed: {exc}")
            return None
