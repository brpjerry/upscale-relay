"""Stage a client-only makepkg build from this checkout, without fetching code."""

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
PACKAGES = ("desktop_client", "relay_client_core", "relay_media", "relay_protocol")


def prepare(destination: Path, tag: str | None = None) -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = metadata["project"]
    version = project["version"]
    if not re.fullmatch(r"[0-9][A-Za-z0-9.]*", version):
        raise ValueError(f"Unsupported pacman release version: {version!r}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} does not match project version {version!r}")
    # Keep the root project's dependency and launcher definitions authoritative.
    quote = json.dumps
    client_metadata = "\n".join([
        "[build-system]",
        f"requires = {quote(metadata['build-system']['requires'])}",
        f"build-backend = {quote(metadata['build-system']['build-backend'])}",
        "[project]",
        'name = "upscale-relay-client"',
        f"version = {quote(version)}",
        'description = "Desktop and headless clients for Upscale Relay"',
        f"requires-python = {quote(project['requires-python'])}",
        f"dependencies = {quote(project['dependencies'] + project['optional-dependencies']['gui'])}",
        "[project.scripts]",
        *(f"{name} = {quote(project['scripts'][name])}"
          for name in ("relay-client", "relay-desktop")),
        "[tool.setuptools.packages.find]",
        f"include = {quote([name + '*' for name in PACKAGES])}",
        "",
    ])
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / "client.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        data = client_metadata.encode()
        info = tarfile.TarInfo("client/pyproject.toml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for package in PACKAGES:
            for path in sorted((ROOT / package).rglob("*.py")):
                archive.add(path, arcname=f"client/{path.relative_to(ROOT)}")
        archive.add(ROOT / "packaging/arch/upscale-relay-client.desktop",
                    arcname="client/upscale-relay-client.desktop")
    recipe = (ROOT / "packaging/arch/PKGBUILD.in").read_text()
    replacements = {
        "VERSION": version,
        "PYTHON_MIN": f"{sys.version_info.major}.{sys.version_info.minor}",
        "PYTHON_MAX": f"{sys.version_info.major}.{sys.version_info.minor + 1}",
        "SHA256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }
    for key, value in replacements.items():
        recipe = recipe.replace(f"@{key}@", value)
    (destination / "PKGBUILD").write_text(recipe)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--tag", help="require an exact match with the release version")
    args = parser.parse_args()
    prepare(args.destination, args.tag)
