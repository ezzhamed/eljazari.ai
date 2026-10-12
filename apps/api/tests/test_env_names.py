"""Tests for the ELJAZARI_/DRONEPROMPT_ environment naming compatibility layer.

The project renamed its env prefix when it settled on eljazari.ai. Renaming the
keys outright would break every existing .env, deployment, and prepared WSL
environment, so both spellings are accepted with the canonical one taking
precedence. These tests pin that contract — a silent regression here would look
like configuration being ignored.
"""

from __future__ import annotations

import pytest

from config.env_names import canonical, env_alias, env_names, getenv, legacy
from config.settings import Settings
from utils.sitl_runtime import _prefixed_override, _string_override

SECRET = "x" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for suffix in ("SITL_RUNTIME", "WSL_USER", "PROBE_FLAG"):
        for name in env_names(suffix):
            monkeypatch.delenv(name, raising=False)
    yield


class TestNaming:
    def test_canonical_and_legacy_prefixes(self):
        assert canonical("SITL_RUNTIME") == "ELJAZARI_SITL_RUNTIME"
        assert legacy("SITL_RUNTIME") == "DRONEPROMPT_SITL_RUNTIME"

    def test_env_names_returns_both(self):
        assert env_names("WSL_USER") == ("ELJAZARI_WSL_USER", "DRONEPROMPT_WSL_USER")

    def test_alias_lists_canonical_first(self):
        """Precedence must be explicit — a deployment setting both gets the new one."""
        assert env_alias("SITL_RUNTIME").choices == [
            "ELJAZARI_SITL_RUNTIME",
            "DRONEPROMPT_SITL_RUNTIME",
        ]


class TestGetenv:
    def test_reads_canonical(self, monkeypatch):
        monkeypatch.setenv("ELJAZARI_PROBE_FLAG", "new")
        assert getenv("PROBE_FLAG") == "new"

    def test_falls_back_to_legacy(self, monkeypatch):
        monkeypatch.setenv("DRONEPROMPT_PROBE_FLAG", "old")
        assert getenv("PROBE_FLAG") == "old"

    def test_canonical_wins_when_both_set(self, monkeypatch):
        monkeypatch.setenv("DRONEPROMPT_PROBE_FLAG", "old")
        monkeypatch.setenv("ELJAZARI_PROBE_FLAG", "new")
        assert getenv("PROBE_FLAG") == "new"

    def test_default_when_neither_set(self):
        assert getenv("PROBE_FLAG", "fallback") == "fallback"
        assert getenv("PROBE_FLAG") is None


class TestSettingsFields:
    """Settings fields carrying env_alias must honour both spellings."""

    def test_legacy_env_still_configures_settings(self, monkeypatch):
        monkeypatch.setenv("DRONEPROMPT_SITL_RUNTIME", "wsl")
        assert Settings(_env_file=None, api_secret_key=SECRET).sitl_runtime == "wsl"

    def test_canonical_env_configures_settings(self, monkeypatch):
        monkeypatch.setenv("ELJAZARI_SITL_RUNTIME", "docker")
        assert Settings(_env_file=None, api_secret_key=SECRET).sitl_runtime == "docker"

    def test_canonical_overrides_legacy(self, monkeypatch):
        monkeypatch.setenv("DRONEPROMPT_SITL_RUNTIME", "wsl")
        monkeypatch.setenv("ELJAZARI_SITL_RUNTIME", "docker")
        assert Settings(_env_file=None, api_secret_key=SECRET).sitl_runtime == "docker"


class TestSitlRuntimeOverrides:
    """_string_override takes an exact name; _prefixed_override takes a suffix.

    Conflating the two silently redirected every DRONEPROMPT_* lookup to
    ELJAZARI_DRONEPROMPT_*, which is how this split came about.
    """

    def test_string_override_uses_the_exact_name(self, monkeypatch):
        monkeypatch.setenv("PX4_DIR", "/opt/px4")
        assert _string_override("PX4_DIR", "fallback") == "/opt/px4"

    def test_string_override_does_not_add_a_prefix(self, monkeypatch):
        monkeypatch.setenv("ELJAZARI_PX4_DIR", "/wrong")
        assert _string_override("PX4_DIR", "fallback") == "fallback"

    def test_prefixed_override_accepts_legacy(self, monkeypatch):
        monkeypatch.setenv("DRONEPROMPT_WSL_USER", "pilot")
        assert _prefixed_override("WSL_USER", "fallback") == "pilot"

    def test_prefixed_override_prefers_canonical(self, monkeypatch):
        monkeypatch.setenv("DRONEPROMPT_WSL_USER", "old")
        monkeypatch.setenv("ELJAZARI_WSL_USER", "new")
        assert _prefixed_override("WSL_USER", "fallback") == "new"

    def test_prefixed_override_falls_back(self):
        assert _prefixed_override("WSL_USER", "fallback") == "fallback"
