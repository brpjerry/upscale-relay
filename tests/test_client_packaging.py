"""Verify the source payload and metadata used by the real pacman build."""

import hashlib
import importlib.util
from pathlib import Path
import tarfile
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_client", ROOT / "packaging/arch/prepare.py")
prepare_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_client)


def test_client_package_contains_only_client_code_and_metadata(tmp_path):
    upstream = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    prepare_client.prepare(tmp_path, f"v{upstream['version']}")
    source = tmp_path / "client.tar.gz"
    with tarfile.open(source) as archive:
        names = archive.getnames()
        assert {name.split('/')[1] for name in names} == {
            "desktop_client", "relay_client_core", "relay_media", "relay_protocol",
            "pyproject.toml", "upscale-relay-client.desktop",
        }
        project = tomllib.loads(archive.extractfile("client/pyproject.toml").read().decode())["project"]
        assert project["version"] == upstream["version"]
        assert project["dependencies"] == upstream["dependencies"] + upstream["optional-dependencies"]["gui"]
        assert project["scripts"] == {
            name: upstream["scripts"][name] for name in ("relay-client", "relay-desktop")
        }
        assert "optional-dependencies" not in project
        assert all("__pycache__" not in name for name in names)
    recipe = (tmp_path / "PKGBUILD").read_text()
    assert hashlib.sha256(source.read_bytes()).hexdigest() in recipe
    assert f"pkgver={upstream['version']}\n" in recipe
    assert "@" not in recipe


def test_release_tag_must_match_packaged_version(tmp_path):
    destination = tmp_path / "build"
    with pytest.raises(ValueError, match="does not match"):
        prepare_client.prepare(destination, "v99999.0.0")
    assert not destination.exists()
