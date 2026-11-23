"""Verify the source payload and metadata used by the real pacman build."""

import hashlib
import importlib.util
from pathlib import Path
import re
import tarfile
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_arch", ROOT / "packaging/arch/prepare.py")
prepare_arch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_arch)

_EXPECTED = {
    "common": ({"relay_media", "relay_protocol"}, set(), set()),
    "client": (
        {"desktop_client", "relay_client_core"},
        {"relay-client", "relay-desktop"},
        {"upscale-relay-client.desktop"},
    ),
    "server": (
        {"relay_server", "upscale_cli"},
        {"relay-server", "relay-server-gui", "upscale-cli"},
        {"upscale-relay-server.service"},
    ),
}


def test_each_package_contains_only_its_code_and_metadata(tmp_path):
    upstream = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    prepare_arch.prepare(tmp_path, f"v{upstream['version']}")
    source = tmp_path / "upscale-relay.tar.gz"
    with tarfile.open(source) as archive:
        names = archive.getnames()
        assert all("__pycache__" not in name for name in names)
        assert {name.split("/")[1] for name in names} == set(_EXPECTED)
        packaged_modules = []
        for part, (modules, commands, data) in _EXPECTED.items():
            top = {name.split("/")[2] for name in names if name.split("/")[1] == part}
            assert top == modules | data | {"pyproject.toml"}, part
            packaged_modules += modules
            project = tomllib.loads(
                archive.extractfile(f"upscale-relay/{part}/pyproject.toml").read().decode()
            )["project"]
            assert project["name"] == f"upscale-relay-{part}"
            assert project["version"] == upstream["version"]
            assert project.get("scripts", {}) == {
                name: upstream["scripts"][name] for name in commands
            }
            assert "optional-dependencies" not in project
            if part == "client":
                assert project["dependencies"] == (
                    upstream["dependencies"] + upstream["optional-dependencies"]["gui"]
                )
        # Every importable package of the project is in exactly one pacman package.
        assert sorted(packaged_modules) == sorted(
            prefix.rstrip("*") for prefix in tomllib.loads(
                (ROOT / "pyproject.toml").read_text()
            )["tool"]["setuptools"]["packages"]["find"]["include"]
        )
    recipe = (tmp_path / "PKGBUILD").read_text()
    assert hashlib.sha256(source.read_bytes()).hexdigest() in recipe
    assert f"pkgver={upstream['version']}\n" in recipe
    assert not re.search(r"@[A-Z_]+@", recipe)  # every placeholder filled
    assert (tmp_path / "upscale-relay-server.install").is_file()


def test_server_package_depends_on_system_cuda():
    # The Linux runtime installs only ONNX Runtime and TensorRT; CUDA 13 and
    # cuDNN 9 are the sonames its providers link against.
    recipe = (ROOT / "packaging/arch/PKGBUILD.in").read_text()
    server = recipe.split("package_upscale-relay-server()", 1)[1]
    for dependency in ("'cudnn'", "'libcudart.so=13-64'", "'libcublas.so=13-64'",
                       "'python-pip'", "'nvidia-utils'", "'ffmpeg'"):
        assert dependency in server


def test_release_tag_must_match_packaged_version(tmp_path):
    destination = tmp_path / "build"
    with pytest.raises(ValueError, match="does not match"):
        prepare_arch.prepare(destination, "v99999.0.0")
    assert not destination.exists()
