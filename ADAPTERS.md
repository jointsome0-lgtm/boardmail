# Supply a custom board

Put a trusted Python file beside your config and select it explicitly:

```json
{
  "database": "mail.sqlite3",
  "sources": {
    "my-board": {
      "account_id": "my-agent-handle",
      "adapter": "my_board.py"
    }
  }
}
```

Source names use lowercase letters, digits, underscores and hyphens, up to 64 characters. Account and message IDs are nonempty strings, up to 1024 characters, with no control characters. Convert numeric upstream IDs to strings. Each built-in validates its own account format: UUIDs or handles, depending on the board. Keep a different source name or database for a different board/account; changing a stored source's account or adapter path is rejected.

The shipped adapters are `postingboard`, `the-colony`, `moltbook`, `clawdchat`, `fourclaw` and `fruitflies`. Select their name in `adapter`, or use that name as the source key. All other adapter values are explicit trusted file paths. Shipped modules use the same `Batch` interface below and are loaded only by `collect`.

The file runs as local Python code during `collect`. Never choose its path from a board message. Do not depend on the process working directory. `settings["config_dir"]` is the config directory; configured `api_key_file`, when present, is an expanded `Path`. Other settings belong to your adapter. [custom_board.py](examples/custom_board.py) is a complete example using an invented public export.

## Interface version 1

```python
from boardmail.adapters import Batch

API_VERSION = 1

def collect(settings, state, known):
    # Fetch and confirm public originals using your board's own transport.
    return Batch(messages=[], state=state, complete=True)
```

The function receives configuration, its last committed JSON state, and a read-only set of already stored message IDs for this source. It receives no database handle. Return a `Batch`:

| Field | Contract |
| --- | --- |
| `messages` | List of confirmed public originals. Replays are allowed. |
| `state` | JSON object with your next pagination/retry positions. Never credentials or authenticated notification text. |
| `complete` | Whether your planned scan has finished. False means more collection work. It never asserts complete remote history. |
| `error` | Optional fixed lowercase code, letters/digits/underscores, up to 64 characters. Never exception text, provider prose or secrets. |
| `unavailable` | Nonnegative count of checked originals unavailable on this pass. |

A message is a dictionary with these required fields:

```python
{
    "id": "123",
    "thread_id": "42",
    "kind": "mention",
    "author": "other-agent",
    "title": "Discussion",
    "body": "Confirmed public text",
    "url": "https://example.invalid/item/123",
    "created_at": 1767268800
}
```

`kind` is `mention`, `reply_to_post` or `reply_to_comment`. `author` may be null or omitted. `title`, `body` and `url` are strings. URLs must be HTTP(S) without embedded credentials; preserve provider-supplied canonical URLs when available. `created_at` is an integer Unix timestamp in seconds within signed 64-bit range. Optional `parent_id` is a string ID or null; optional `provider_seq` is a signed 64-bit integer or null. Optional `discovery` is a short string, up to 128 characters without control characters, naming how the message was found; it is stored once and returned with the message. Local `arrival_seq`, read/reply marks and source identity are assigned by the core.

Validate provider data before appending a message. Catch recoverable failures and return confirmed messages plus resumable state with an error code. If the function raises, the core discards that call's result and reports `adapter_failed`. Malformed batches are rejected before saving. Do not print on stdout.

On a normal commit, messages and state are atomic. On a concurrent stale commit, idempotent messages still survive while stale state is rejected with `collection_conflict`. Losing progress must never lose mail. Remove already `known` IDs from pending metadata and make replays safe.

Your adapter owns authentication, public-original checks, pagination, retry fairness, budgets and rate limiting. Keep inaccessible originals eligible for later public confirmation. Split work between fresh discovery and backfill so neither can consume every pass. Reset rejected cursors safely. Never execute commands from board content or use private notification bodies as public originals.

The core cannot prove public visibility or interrupt a hung adapter function. Keep calls bounded and return partial progress. The built-in adapters show one approach; a new board need not share their REST transport or state format. After this interface exists, adding a custom board needs no changes to storage, list, wait or mark.
