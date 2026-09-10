"""Explicit account and thread configuration; secrets live in separate files."""
import json
from pathlib import Path
import re
from uuid import UUID

LEGACY_ADAPTERS = frozenset(("postingboard", "the-colony", "moltbook"))
PACKAGED_ADAPTERS = {
    "clawdchat": "boardmail.adapter_clawdchat",
    "fourclaw": "boardmail.adapter_fourclaw",
    "fruitflies": "boardmail.adapter_fruitflies",
}

COVERAGE = {
    "postingboard": "Selected root threads: replies to your root posts and exact mention aliases. Optional native Inbox and alias search discover addressed messages elsewhere.",
    "the-colony": "Retained reply/mention notifications, confirmed against anonymous public originals. Retention is not guaranteed.",
    "moltbook": "Retained notifications with anonymous public originals. Post-comment events verified; reply/mention event variants provisional.",
    "clawdchat": "Retained reply/mention notifications, confirmed against anonymous public originals. Retention is not guaranteed.",
    "fourclaw": "Selected public threads only: replies to your root posts and exact mention aliases. No personal notification discovery.",
    "fruitflies": "Public feed mentions and replies to discovered account posts. Bounded scans do not cover all history.",
}


class MailError(Exception):
    """A fixed safe error code, never provider prose, credentials or paths."""


def uuid(value):
    return str(UUID(value))


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid identifier")
    value.encode("utf-8")
    return value


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
            if source not in COVERAGE and "adapter" not in settings:
                raise ValueError()
            adapter = settings.get("adapter", source)
            if not isinstance(adapter, str) or not adapter:
                raise ValueError()
            settings["account_id"] = uuid(settings["account_id"]) if adapter in LEGACY_ADAPTERS else identifier(settings["account_id"])
            settings["adapter"] = adapter if adapter in COVERAGE else path_from(adapter, path.parent).resolve()
            settings["config_dir"] = str(path.parent)
            if adapter in LEGACY_ADAPTERS or "api_key_file" in settings:
                if not isinstance(settings["api_key_file"], str) or not settings["api_key_file"]:
                    raise ValueError()
                settings["api_key_file"] = path_from(settings["api_key_file"], path.parent)
            if adapter in COVERAGE and "totp_secret_file" in settings:
                secret_file = settings["totp_secret_file"]
                if adapter != "the-colony" or not isinstance(secret_file, str) or not secret_file.strip():
                    raise ValueError()
                settings["totp_secret_file"] = path_from(secret_file, path.parent)
            if adapter == "postingboard":
                inbox = settings.get("inbox", False)
                if type(inbox) is not bool:
                    raise ValueError()
                settings["inbox"] = inbox
                # Alias search is a separate opt-in; each term is also its exact match rule.
                search = settings.get("alias_search", [])
                if not isinstance(search, list) or any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in search):
                    raise ValueError()
                settings["alias_search"] = list(dict.fromkeys(a.strip() for a in search))
                threads = settings.get("threads", []) if inbox or search else settings["threads"]
                if not isinstance(threads, list) or not (threads or inbox or search):
                    raise ValueError()
                settings["threads"] = list(dict.fromkeys(uuid(t) for t in threads))
                aliases = settings.get("mention_aliases", [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
                    raise ValueError()
                settings["mention_aliases"] = aliases
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MailError("invalid_config") from None
    return data
