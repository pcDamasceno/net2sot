"""
load_settings() merges settings.yaml with environment overrides and normalizes
the NetBox URL. A URL that keeps its trailing slash builds request paths and
error messages like ".../" + "/api/", so the normalization is what stops a
pasted browser URL from reading as a different host than the configured one.
"""

import textwrap

import pytest

from net2sot.discovery import load_settings


@pytest.fixture
def settings_file(tmp_path):
    """Write a minimal settings.yaml and return its path."""

    def _write(body: str) -> str:
        path = tmp_path / "settings.yaml"
        path.write_text(textwrap.dedent(body))
        return str(path)

    return _write


@pytest.fixture(autouse=True)
def _clear_netbox_env(monkeypatch):
    """Keep the developer's own NETBOX_* exports out of these assertions."""
    for var in ("NETBOX_URL", "NETBOX_TOKEN", "NETBOX_BRANCH"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://netbox.example.com", "https://netbox.example.com"),
        ("https://netbox.example.com/", "https://netbox.example.com"),
        ("https://netbox.example.com///", "https://netbox.example.com"),
        ("  https://netbox.example.com/  ", "https://netbox.example.com"),
        # A URL with a path prefix keeps the path, loses only the trailing slash.
        ("https://example.com/netbox/", "https://example.com/netbox"),
    ],
)
def test_netbox_url_trailing_slash_stripped_from_env(
    settings_file, monkeypatch, raw, expected
):
    path = settings_file('netbox_url: "https://from-file.example.com"\n')
    monkeypatch.setenv("NETBOX_URL", raw)

    assert load_settings(path)["netbox_url"] == expected


def test_netbox_url_trailing_slash_stripped_from_settings_file(settings_file):
    """Normalization applies to the file too, not just the env override."""
    path = settings_file('netbox_url: "https://netbox.example.com/"\n')

    assert load_settings(path)["netbox_url"] == "https://netbox.example.com"


def test_env_overrides_settings_file(settings_file, monkeypatch):
    path = settings_file('netbox_url: "https://from-file.example.com"\n')
    monkeypatch.setenv("NETBOX_URL", "https://from-env.example.com/")

    assert load_settings(path)["netbox_url"] == "https://from-env.example.com"


def test_missing_netbox_url_is_left_alone(settings_file):
    """No URL configured stays absent, for validate_settings to report."""
    path = settings_file('site_name: "lab"\n')

    assert load_settings(path).get("netbox_url") is None
