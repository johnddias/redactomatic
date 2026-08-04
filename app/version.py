"""Build/version identifier shown in the UI footer.

Docker builds bake the git commit into a ``VERSION`` file at image-build
time (see Dockerfile) since the final image doesn't ship a ``.git``
directory. Local/dev runs fall back to asking git directly. Neither
being available (e.g. a source tarball with no git history) falls back
to "dev" rather than failing -- the version string is a debugging aid,
not something anything else depends on.
"""

import pathlib
import subprocess

_VERSION_FILE = pathlib.Path(__file__).with_name("VERSION")


def get_version() -> str:
    if _VERSION_FILE.exists():
        version = _VERSION_FILE.read_text(encoding="utf-8").strip()
        if version:
            return version

    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=pathlib.Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        version = result.stdout.strip()
        if version:
            return version
    except (OSError, subprocess.SubprocessError):
        pass

    return "dev"


VERSION = get_version()
