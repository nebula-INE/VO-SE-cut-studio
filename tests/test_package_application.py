import pytest

from scripts.package_application import macos_zip_enabled


def test_macos_zip_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("AXRAL_MACOS_INCLUDE_ZIP", raising=False)

    assert macos_zip_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_macos_zip_accepts_explicit_truthy_values(monkeypatch, value):
    monkeypatch.setenv("AXRAL_MACOS_INCLUDE_ZIP", value)

    assert macos_zip_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_macos_zip_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("AXRAL_MACOS_INCLUDE_ZIP", value)

    assert macos_zip_enabled() is False
