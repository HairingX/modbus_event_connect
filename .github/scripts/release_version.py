"""The release version: check it is PEP 440 in canonical form, and write it into the package.

    release_version.py check VERSION        prints version=, tag= and prerelease= lines
    release_version.py write VERSION FILE   sets the one `__version__ = "..."` line in FILE
"""
import re
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

_LINE = re.compile(r'^__version__ = "[^"\n]*"$', re.MULTILINE)


def _canonical(text: str) -> Version:
    """The version `text` names, refused unless `text` is already its canonical form."""
    try:
        version = Version(text)
    except InvalidVersion:
        sys.exit(f"::error::{text!r} is not a PEP 440 version, such as 1.2.3 or 1.2.3rc1")
    if str(version) != text:
        # A build writes the canonical form into the file names, so any other spelling would
        # tag one version and publish another.
        sys.exit(f"::error::write {text!r} as {str(version)!r}, its canonical PEP 440 form")
    if len(version.release) != 3 or version.epoch or version.local:
        # 1.2 and 1.2.0 are one version to PEP 440 but two tags; PyPI refuses a local version.
        sys.exit(f"::error::{text!r} must be MAJOR.MINOR.PATCH, optionally with a pre-release, "
                 f"such as 1.2.3 or 1.2.3rc1")
    return version


def check(text: str) -> None:
    version = _canonical(text)
    print(f"version={version}")
    print(f"tag=v{version}")
    print(f"prerelease={'true' if version.is_prerelease else 'false'}")


def write(text: str, path: Path) -> None:
    version = _canonical(text)
    source = path.read_text(encoding="utf-8")
    found = _LINE.findall(source)
    if len(found) != 1:
        sys.exit(f"::error::{path} must hold exactly one `__version__ = \"...\"` line, found {len(found)}")
    path.write_text(_LINE.sub(f'__version__ = "{version}"', source), encoding="utf-8")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["check", text]:
            check(text)
        case ["write", text, path]:
            write(text, Path(path))
        case _:
            sys.exit(__doc__)
