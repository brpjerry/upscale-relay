"""Stage the Arch split-package build from this checkout, without fetching code.

One PKGBUILD builds three packages from one source archive:

- upscale-relay-common: relay_protocol and relay_media, which both sides import.
- upscale-relay-client: the desktop player and the headless client.
- upscale-relay-server: the relay server, its inference worker and upscale-cli.

pacman lets only one package own a file, so the shared modules get their own
package rather than a copy in each.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import tomllib


ROOT = Path(__file__).resolve().parents[2]
ARCH = ROOT / "packaging" / "arch"
SOURCE_DIR = "upscale-relay"

# part: (package name, description, modules, commands, data files)
PARTS = {
    "common": (
        "upscale-relay-common",
        "Shared protocol and media modules for Upscale Relay",
        ("relay_media", "relay_protocol"),
        (),
        (),
    ),
    "client": (
        "upscale-relay-client",
        "Desktop and headless clients for Upscale Relay",
        ("desktop_client", "relay_client_core"),
        ("relay-client", "relay-desktop"),
        ("upscale-relay-client.desktop",),
    ),
    "server": (
        "upscale-relay-server",
        "Upscale Relay server: ONNX upscaling with TensorRT and NVENC",
        ("relay_server", "upscale_cli"),
        ("relay-server", "relay-server-gui", "upscale-cli"),
        ("upscale-relay-server.service",),
    ),
}


def _dependencies(project: dict, part: str) -> list[str]:
    """Python metadata only; pacman's depends are in PKGBUILD.in."""
    if part == "common":
        # relay_protocol and relay_media import only PyAV beyond the stdlib.
        return [dep for dep in project["dependencies"] if re.match(r"av\b", dep)]
    dependencies = [*project["dependencies"], f"upscale-relay-common=={project['version']}"]
    if part == "client":
        dependencies += project["optional-dependencies"]["gui"]
    return dependencies


def _metadata(metadata: dict, part: str) -> str:
    project = metadata["project"]
    name, description, modules, commands, _ = PARTS[part]
    quote = json.dumps
    lines = [
        "[build-system]",
        f"requires = {quote(metadata['build-system']['requires'])}",
        f"build-backend = {quote(metadata['build-system']['build-backend'])}",
        "[project]",
        f"name = {quote(name)}",
        f"version = {quote(project['version'])}",
        f"description = {quote(description)}",
        f"requires-python = {quote(project['requires-python'])}",
        f"dependencies = {quote(_dependencies(project, part))}",
    ]
    if commands:
        # Keep the root project's launcher definitions authoritative.
        lines.append("[project.scripts]")
        lines += [f"{command} = {quote(project['scripts'][command])}" for command in commands]
    lines += [
        "[tool.setuptools.packages.find]",
        f"include = {quote([module + '*' for module in modules])}",
        "",
    ]
    return "\n".join(lines)


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def prepare(destination: Path, tag: str | None = None) -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    if not re.fullmatch(r"[0-9][A-Za-z0-9.]*", version):
        raise ValueError(f"Unsupported pacman release version: {version!r}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} does not match project version {version!r}")
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / f"{SOURCE_DIR}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for part, (_, _, modules, _, data_files) in PARTS.items():
            base = f"{SOURCE_DIR}/{part}"
            _add_bytes(archive, f"{base}/pyproject.toml", _metadata(metadata, part).encode())
            for module in modules:
                for path in sorted((ROOT / module).rglob("*.py")):
                    archive.add(path, arcname=f"{base}/{path.relative_to(ROOT)}")
            for name in data_files:
                archive.add(ARCH / name, arcname=f"{base}/{name}")
    recipe = (ARCH / "PKGBUILD.in").read_text()
    replacements = {
        "VERSION": version,
        "PYTHON_MIN": f"{sys.version_info.major}.{sys.version_info.minor}",
        "PYTHON_MAX": f"{sys.version_info.major}.{sys.version_info.minor + 1}",
        "SHA256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }
    for key, value in replacements.items():
        recipe = recipe.replace(f"@{key}@", value)
    (destination / "PKGBUILD").write_text(recipe)
    # makepkg reads install scriptlets beside the PKGBUILD, not from source.
    (destination / "upscale-relay-server.install").write_text(
        (ARCH / "upscale-relay-server.install").read_text()
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--tag", help="require an exact match with the release version")
    args = parser.parse_args()
    prepare(args.destination, args.tag)
