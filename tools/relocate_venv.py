"""Repair a virtualenv that was moved or renamed on disk.

A venv is not relocatable: `Scripts\\*.exe` console-script launchers embed the
absolute path of the interpreter that generated them, and the activate scripts
plus `pyvenv.cfg` record the old prefix. After a move the launchers exit with
status 1 and print nothing, while `Scripts\\python.exe` still works — it finds
its home through the adjacent `pyvenv.cfg`.

A pip launcher is `stub + b"#!<interpreter>\\r\\n" + zip payload`; zipimport
tolerates an arbitrary-length prefix, so substituting a shorter or longer path
is safe. Recreating the venv is still the supported fix — this exists for the
case where redownloading multi-gigabyte wheels is not worth it.

Usage: python tools/relocate_venv.py OLD_PREFIX [VENV_DIR] [--dry-run]
"""

import sys
from pathlib import Path

_TEXT_FILES = ("pyvenv.cfg", "Scripts/activate", "Scripts/activate.bat", "Scripts/activate.fish", "Scripts/Activate.ps1")


def relocate(venv: Path, old: str, dry_run: bool = False) -> None:
    new = str(venv.resolve())
    old = old.rstrip("\\/")
    if old == new:
        print(f"nothing to do: {venv} already reports {new}")
        return

    for exe in sorted(venv.glob("Scripts/*.exe")):
        blob = exe.read_bytes()
        if old.encode() not in blob:
            continue
        print(f"launcher  {exe.name}")
        if not dry_run:
            exe.write_bytes(blob.replace(old.encode(), new.encode()))

    # The activate scripts mix separator styles; pyvenv.cfg records the
    # original `python -m venv <path>` command line.
    for rel in _TEXT_FILES:
        target = venv / rel
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8")
        fixed = text.replace(old, new).replace(old.replace("\\", "/"), new.replace("\\", "/"))
        if fixed == text:
            continue
        print(f"text      {rel}")
        if not dry_run:
            target.write_text(fixed, encoding="utf-8")

    print("dry run — nothing written" if dry_run else f"relocated to {new}")
    print("now rerun `python -m pip install -e . --no-deps --no-build-isolation` "
          "if this venv has an editable install of a moved source tree")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        sys.exit(__doc__.strip().splitlines()[-1])
    relocate(Path(args[1]) if len(args) > 1 else Path(sys.prefix), args[0], "--dry-run" in sys.argv)
