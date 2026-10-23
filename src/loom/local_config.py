"""Read project-owned secrets without importing them into the process environment."""
import json
import os
from collections.abc import Mapping
from pathlib import Path
import re
from types import MappingProxyType


class PrivateConfigError(ValueError):
    def __init__(self):
        super().__init__("PRIVATE_CONFIG_INVALID")


class ProjectSecrets(Mapping):
    __slots__ = ("_data",)

    def __init__(self, data):
        self._data = MappingProxyType(dict(data))

    def __getitem__(self, name):
        return self._data[name]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return "<ProjectSecrets redacted>"


def load_project_secrets(path: str | Path, *, allowed_names: set[str]) -> ProjectSecrets:
    """Credentials must remain outside the source tree and public output folder.

    The operator protects the file with OS permissions. Values are returned in
    a separate mapping; subprocesses and Docker never inherit them via os.environ.
    """
    try:
        supplied = Path(path)
        resolved = supplied.resolve(strict=True)
        project = Path(__file__).resolve().parents[2]
        public_outputs = project.parent
        if supplied.is_symlink() or resolved.is_relative_to(public_outputs) or not resolved.is_file():
            raise PrivateConfigError()
        if os.name != "nt" and resolved.stat().st_mode & 0o077:
            raise PrivateConfigError()
        with resolved.open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise PrivateConfigError()

        def pairs(items):
            result = {}
            for name, value in items:
                if name in result:
                    raise PrivateConfigError()
                result[name] = value
            return result

        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
        if (not isinstance(data, dict) or not data or not set(data) <= allowed_names
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name)
                       or not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,8192}", value)
                       for name, value in data.items())):
            raise PrivateConfigError()
        return ProjectSecrets(data)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise PrivateConfigError() from None
