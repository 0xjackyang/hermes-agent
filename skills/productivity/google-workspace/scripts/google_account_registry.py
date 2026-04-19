#!/usr/bin/env python3
"""Helpers for resolving Google Workspace account selection within a Hermes profile."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ACCOUNT_ENV_VAR = "HERMES_GOOGLE_ACCOUNT"
ROUTE_ENV_VAR = "HERMES_GOOGLE_ROUTE"
REGISTRY_FILENAME = "google_accounts.json"
LEGACY_TOKEN_FILENAME = "google_token.json"
LEGACY_CLIENT_SECRET_FILENAME = "google_client_secret.json"
LEGACY_PENDING_FILENAME = "google_oauth_pending.json"


@dataclass(frozen=True)
class GoogleAccountSelection:
    alias: str
    email: str
    token_path: Path
    client_secret_path: Path
    pending_auth_path: Path
    selection_source: str
    route: str = ""
    registry_path: Path | None = None

    @property
    def is_legacy(self) -> bool:
        return self.selection_source.startswith("legacy")


def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def get_registry_path(hermes_home: Path | None = None) -> Path:
    hermes_home = hermes_home or get_hermes_home()
    return hermes_home / REGISTRY_FILENAME


def _legacy_selection(*, hermes_home: Path, source: str, route: str = "", registry_path: Path | None = None) -> GoogleAccountSelection:
    return GoogleAccountSelection(
        alias="",
        email="",
        token_path=hermes_home / LEGACY_TOKEN_FILENAME,
        client_secret_path=hermes_home / LEGACY_CLIENT_SECRET_FILENAME,
        pending_auth_path=hermes_home / LEGACY_PENDING_FILENAME,
        selection_source=source,
        route=route,
        registry_path=registry_path,
    )


def _resolve_relative_path(hermes_home: Path, relative_path: str, *, field_name: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError(f"{field_name} must be relative to HERMES_HOME, got absolute path: {relative_path}")
    resolved = (hermes_home / candidate).resolve()
    hermes_home_resolved = hermes_home.resolve()
    try:
        resolved.relative_to(hermes_home_resolved)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} escapes HERMES_HOME: {relative_path} -> {resolved}"
        ) from exc
    return resolved


def _load_registry(hermes_home: Path) -> tuple[dict, Path | None]:
    registry_path = get_registry_path(hermes_home)
    if not registry_path.exists():
        return {}, None
    payload = json.loads(registry_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"google_accounts.json must parse to an object: {registry_path}")
    return payload, registry_path


def _default_pending_relative(token_relative: str, alias: str) -> str:
    token_candidate = Path(token_relative) if token_relative else Path("google-accounts") / f"{alias}.json"
    pending_name = f"{token_candidate.stem}_oauth_pending.json"
    return str(token_candidate.with_name(pending_name))


def _selection_from_account_entry(
    *,
    hermes_home: Path,
    alias: str,
    account_entry: dict,
    source: str,
    registry_path: Path | None,
    route: str = "",
) -> GoogleAccountSelection:
    token_relative = (account_entry.get("token_path") or f"google-accounts/{alias}.json").strip()
    client_secret_relative = (account_entry.get("client_secret_path") or LEGACY_CLIENT_SECRET_FILENAME).strip()
    pending_relative = (account_entry.get("pending_auth_path") or _default_pending_relative(token_relative, alias)).strip()
    email = (account_entry.get("email") or "").strip()

    return GoogleAccountSelection(
        alias=alias,
        email=email,
        token_path=_resolve_relative_path(hermes_home, token_relative, field_name=f"accounts.{alias}.token_path"),
        client_secret_path=_resolve_relative_path(hermes_home, client_secret_relative, field_name=f"accounts.{alias}.client_secret_path"),
        pending_auth_path=_resolve_relative_path(hermes_home, pending_relative, field_name=f"accounts.{alias}.pending_auth_path"),
        selection_source=source,
        route=route,
        registry_path=registry_path,
    )


def _selection_from_alias(
    *,
    hermes_home: Path,
    registry: dict,
    registry_path: Path | None,
    alias: str,
    source: str,
    route: str = "",
    allow_conventional_fallback: bool = False,
) -> GoogleAccountSelection:
    accounts = registry.get("accounts")
    if isinstance(accounts, dict) and alias in accounts:
        account_entry = accounts.get(alias)
        if not isinstance(account_entry, dict):
            raise ValueError(f"google_accounts.json entry for alias {alias!r} must be an object")
        return _selection_from_account_entry(
            hermes_home=hermes_home,
            alias=alias,
            account_entry=account_entry,
            source=source,
            registry_path=registry_path,
            route=route,
        )
    if allow_conventional_fallback:
        return _selection_from_account_entry(
            hermes_home=hermes_home,
            alias=alias,
            account_entry={},
            source=f"{source}_conventional",
            registry_path=registry_path,
            route=route,
        )
    raise ValueError(f"Unknown Google account alias: {alias}")


def resolve_google_account_selection(account: str = "", route: str = "") -> GoogleAccountSelection:
    hermes_home = get_hermes_home()
    registry, registry_path = _load_registry(hermes_home)
    legacy = _legacy_selection(hermes_home=hermes_home, source="legacy_default", registry_path=registry_path)

    explicit_account = account.strip()
    explicit_route = route.strip()

    if explicit_account:
        return _selection_from_alias(
            hermes_home=hermes_home,
            registry=registry,
            registry_path=registry_path,
            alias=explicit_account,
            source="explicit_account",
            allow_conventional_fallback=True,
        )

    if explicit_route:
        routes = registry.get("routes") if isinstance(registry.get("routes"), dict) else {}
        if explicit_route not in routes:
            raise ValueError(f"Unknown Google account route: {explicit_route}")
        alias = str(routes[explicit_route]).strip()
        if not alias:
            raise ValueError(f"Google account route {explicit_route!r} does not map to an alias")
        return _selection_from_alias(
            hermes_home=hermes_home,
            registry=registry,
            registry_path=registry_path,
            alias=alias,
            source="explicit_route",
            route=explicit_route,
        )

    env_account = os.environ.get(ACCOUNT_ENV_VAR, "").strip()
    if env_account:
        return _selection_from_alias(
            hermes_home=hermes_home,
            registry=registry,
            registry_path=registry_path,
            alias=env_account,
            source="env_account",
            allow_conventional_fallback=True,
        )

    env_route = os.environ.get(ROUTE_ENV_VAR, "").strip()
    if env_route:
        routes = registry.get("routes") if isinstance(registry.get("routes"), dict) else {}
        if env_route not in routes:
            raise ValueError(f"Unknown Google account route from {ROUTE_ENV_VAR}: {env_route}")
        alias = str(routes[env_route]).strip()
        if not alias:
            raise ValueError(f"Google account route {env_route!r} does not map to an alias")
        return _selection_from_alias(
            hermes_home=hermes_home,
            registry=registry,
            registry_path=registry_path,
            alias=alias,
            source="env_route",
            route=env_route,
        )

    routes = registry.get("routes") if isinstance(registry.get("routes"), dict) else {}
    default_alias = str(routes.get("interactive_default") or registry.get("default") or "").strip()
    if default_alias:
        default_selection = _selection_from_alias(
            hermes_home=hermes_home,
            registry=registry,
            registry_path=registry_path,
            alias=default_alias,
            source="registry_default",
            route="interactive_default" if str(routes.get("interactive_default") or "").strip() else "",
        )
        if default_selection.token_path.exists() or not legacy.token_path.exists():
            return default_selection
        return _legacy_selection(
            hermes_home=hermes_home,
            source="legacy_default_fallback",
            route=default_selection.route,
            registry_path=registry_path,
        )

    return legacy
