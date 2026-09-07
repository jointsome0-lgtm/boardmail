"""A separately supplied adapter for an invented, local public-board export."""
import json
from pathlib import Path

from boardmail.adapters import Batch

API_VERSION = 1


def collect(settings, state, known):
    # A real adapter fetches originals using its own transport/authentication.
    # This example only reads an explicitly supplied synthetic public export.
    feed = Path(settings["config_dir"])/settings["feed_file"]
    items = json.loads(feed.read_text())["items"]
    start = state.get("offset", 0)
    if start >= len(items): start = 0
    size = settings.get("batch_size", 10)
    if type(size) is not int or size < 1: raise ValueError("Invalid batch size")
    end = min(start+size, len(items))
    messages = []
    for item in items[start:end]:
        if not item["public"] or str(item["id"]) in known: continue
        messages.append({"id": str(item["id"]), "thread_id": str(item["thread_id"]),
            "kind": "mention", "author": item["author"], "title": item["title"],
            "body": item["body"], "created_at": item["created_at"], "url": item["web_url"]})
    return Batch(messages=messages, state={"offset": end if end < len(items) else 0},
                 complete=end == len(items))
