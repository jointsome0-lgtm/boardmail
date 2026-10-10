"""Explicit account and thread configuration; secrets live in separate files."""
import json
import os
from pathlib import Path
import re

from .boards import BOARDS, owner
from .errors import MailError, converted, identifier, uuid  # Their callers find them here as well.

MAX_ALIAS = 100
# The folder beside a config in which a source finds its adapter file where its settings name none.
ADAPTERS = "adapters"


def path_from(value, base):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def load(path):
    path = Path(path).expanduser().resolve()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise MailError("config_missing") from None
    except (OSError, ValueError):
        raise MailError("invalid_config") from None
    try:
        if not isinstance(data["database"], str) or not data["database"]:
            raise ValueError()
        data["database"] = path_from(data["database"], path.parent)
        sources = data["sources"]
        if not isinstance(sources, dict) or not sources:
            raise ValueError()
        for source, settings in sources.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source) or not isinstance(settings, dict):
                raise ValueError()
            # An adapter that the settings name is text. A source that names none is a board of the package, or
            # has its file in the folder for adapter files, under its own name.
            if not isinstance(settings.get("adapter", source), str):
                raise ValueError()
            if source not in BOARDS and "adapter" not in settings:
                found = path.parent / ADAPTERS / f"{source}.py"
                # Not found.is_file(): before Python 3.14 that raises where the folder cannot be entered.
                if not os.path.isfile(found):
                    raise ValueError()
                settings["adapter"] = str(found)
            adapter = owner(source, settings)
            # What that board declares, or None for the file of an operator, whose own settings pass unchanged.
            board = BOARDS.get(adapter)
            if not adapter or board and (settings.keys() - board.fields - {"account_id", "adapter"}
                                         or board.required - settings.keys()):
                raise ValueError()
            settings["account_id"] = (board.account if board else identifier)(settings["account_id"])
            settings["adapter"] = adapter if board else path_from(adapter, path.parent).resolve()
            settings["config_dir"] = str(path.parent)
            if "api_key_file" in settings:
                if not isinstance(settings["api_key_file"], str) or not settings["api_key_file"]:
                    raise ValueError()
                settings["api_key_file"] = path_from(settings["api_key_file"], path.parent)
            if board and "totp_secret_file" in settings:
                secret_file = settings["totp_secret_file"]
                if not isinstance(secret_file, str) or not secret_file.strip():
                    raise ValueError()
                settings["totp_secret_file"] = path_from(secret_file, path.parent)
            if board and board.configure:
                board.configure(settings)
            if board and "mention_aliases" in settings:
                # One rule for every built-in adapter: non-blank text, bounded, deduplicated.
                # Adapters add their own stricter name rules on top.
                aliases = settings["mention_aliases"]
                if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() or len(a) > MAX_ALIAS for a in aliases):
                    raise ValueError()
                settings["mention_aliases"] = list(dict.fromkeys(a.strip() for a in aliases))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MailError("invalid_config") from None
    return data
