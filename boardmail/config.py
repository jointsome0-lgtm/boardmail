"""Explicit account and thread configuration; secrets live in separate files."""
import json
from pathlib import Path
from uuid import UUID

COVERAGE = {
    "postingboard": "Selected root threads only: replies to your root posts and exact mention aliases. No comment-parent signal.",
    "the-colony": "Retained reply/mention notifications, confirmed against anonymous public originals. Retention is not guaranteed.",
    "moltbook": "Retained notifications with anonymous public originals. Post-comment events verified; reply/mention event variants provisional.",
}


class MailError(Exception):
    """A fixed safe error code, never provider prose, credentials or paths."""


def uuid(value):
    return str(UUID(value))


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
        if not isinstance(sources, dict) or not sources or not sources.keys() <= COVERAGE.keys():
            raise ValueError()
        for source, settings in sources.items():
            settings["account_id"] = uuid(settings["account_id"])
            if not isinstance(settings["api_key_file"], str) or not settings["api_key_file"]:
                raise ValueError()
            settings["api_key_file"] = path_from(settings["api_key_file"], path.parent)
            if source == "postingboard":
                if not isinstance(settings["threads"], list) or not settings["threads"]:
                    raise ValueError()
                settings["threads"] = list(dict.fromkeys(uuid(t) for t in settings["threads"]))
                aliases = settings.get("mention_aliases", [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
                    raise ValueError()
                settings["mention_aliases"] = aliases
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MailError("invalid_config") from None
    return data
