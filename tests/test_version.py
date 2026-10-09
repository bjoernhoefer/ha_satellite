"""The version must be a valid semver and identical in all places."""

import re
import tomllib
from pathlib import Path

import ha_satimage

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_is_semver():
    assert SEMVER.match(ha_satimage.__version__)


def test_version_matches_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["version"] == ha_satimage.__version__


def test_version_listed_in_changelog():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{ha_satimage.__version__}]" in changelog
