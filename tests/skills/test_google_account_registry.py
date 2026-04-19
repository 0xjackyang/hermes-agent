"""Tests for Google Workspace account registry resolution."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/google_account_registry.py"
)


@pytest.fixture
def registry_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("google_account_registry_test", REGISTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_registry(hermes_home: Path) -> None:
    payload = {
        "default": "satori",
        "accounts": {
            "satori": {
                "email": "satori@jackyang.com",
                "token_path": "google-accounts/satori.json",
                "client_secret_path": "google_client_secret.json",
            },
            "jack-electrum": {
                "email": "jack@electrum.id",
                "token_path": "google-accounts/jack-electrum.json",
                "client_secret_path": "google_client_secret.json",
            },
        },
        "routes": {
            "interactive_default": "satori",
            "electrum_docs_drive": "jack-electrum",
        },
    }
    (hermes_home / "google_accounts.json").write_text(json.dumps(payload))


def test_legacy_default_without_registry(registry_module):
    selection = registry_module.resolve_google_account_selection()
    assert selection.alias == ""
    assert selection.selection_source == "legacy_default"
    assert selection.token_path.name == "google_token.json"


def test_explicit_account_uses_conventional_fallback_when_registry_missing(registry_module):
    selection = registry_module.resolve_google_account_selection(account="jack-electrum")
    assert selection.alias == "jack-electrum"
    assert selection.selection_source == "explicit_account_conventional"
    assert selection.token_path.name == "jack-electrum.json"
    assert selection.pending_auth_path.name == "jack-electrum_oauth_pending.json"


def test_explicit_route_uses_registry_mapping(registry_module):
    hermes_home = Path(os.environ["HERMES_HOME"])
    _write_registry(hermes_home)

    selection = registry_module.resolve_google_account_selection(route="electrum_docs_drive")
    assert selection.alias == "jack-electrum"
    assert selection.route == "electrum_docs_drive"
    assert selection.selection_source == "explicit_route"
    assert selection.email == "jack@electrum.id"
    assert selection.token_path == hermes_home / "google-accounts/jack-electrum.json"


def test_registry_default_falls_back_to_legacy_root_when_alias_token_missing(registry_module):
    hermes_home = Path(os.environ["HERMES_HOME"])
    _write_registry(hermes_home)
    (hermes_home / "google_token.json").write_text("{}")

    selection = registry_module.resolve_google_account_selection()
    assert selection.alias == ""
    assert selection.selection_source == "legacy_default_fallback"
    assert selection.token_path == hermes_home / "google_token.json"


def test_env_account_overrides_registry_default(registry_module, monkeypatch):
    hermes_home = Path(os.environ["HERMES_HOME"])
    _write_registry(hermes_home)
    monkeypatch.setenv("HERMES_GOOGLE_ACCOUNT", "jack-electrum")

    selection = registry_module.resolve_google_account_selection()
    assert selection.alias == "jack-electrum"
    assert selection.selection_source == "env_account"


def test_unknown_route_raises_clean_error(registry_module):
    with pytest.raises(ValueError, match="Unknown Google account route"):
        registry_module.resolve_google_account_selection(route="does-not-exist")
