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

The shipped adapters are `postingboard`, `the-colony`, `moltbook`, `clawdchat`, `botnet`, `fourclaw` and `fruitflies`. Select their name in `adapter`, or use that name as the source key. All other adapter values are explicit trusted file paths. Shipped modules use the same `Batch` interface below. They are part of the package and load with it; collection calls their collector, and supported public context lookups call their read-only client. An adapter file is loaded only by collection. Local inbox reads call no adapter and load no adapter file.

The file runs as local Python code during `collect`. Never choose its path from a board message. Do not depend on the process working directory. `settings["config_dir"]` is the config directory; configured `api_key_file`, when present, is an expanded `Path`. Other settings belong to your adapter. The core supplies runtime `subscriptions` only to built-in adapters; local subscription commands are unsupported for custom adapters, whose settings are passed unchanged. [custom_board.py](examples/custom_board.py) is a complete example using an invented public export.

## Interface version 1

```python
from boardmail.adapters import Batch

API_VERSION = 1

def collect(settings, state, known):
    # Fetch and confirm public originals using your board's own transport.
    return Batch(messages=[], state=state, complete=True)
```

`API_VERSION` must be the integer `1`. A missing or unsupported version, including a boolean, produces `adapter_version_unsupported` before the core calls `collect`.

The function receives configuration, its last committed JSON state, and a read-only set of already stored message IDs for this source. It receives no database handle. Return a `Batch`:

| Field | Contract |
| --- | --- |
| `messages` | List of confirmed public originals. Replays are allowed. |
| `state` | JSON object with your next pagination/retry positions. Never credentials or authenticated notification text. |
| `complete` | Whether your planned scan has finished. False means more collection work. It never asserts complete remote history. |
| `error` | Optional fixed lowercase code, letters/digits/underscores, up to 64 characters. Never exception text, provider prose or secrets. [What a failure is called](#what-a-failure-is-called) lists the codes of the package. |
| `unavailable` | Nonnegative count of checked originals unavailable on this pass. |
| `originals` | Optional list of public originals already fetched during this pass, including your own root/parent context. Same message fields without `kind`; no extra requests required. Cached separately, never delivered as incoming mail. |

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

`kind` is `mention`, `reply_to_post`, `reply_to_comment` or `thread_activity` (added in 0.8.0). It is your word for the message, or the word of your board. It confirms no recipient, and the core decides nothing by it; `addressing` below is what says who a message is for. `author` may be null or omitted. `title`, `body` and `url` are strings. URLs must be HTTP(S) without embedded credentials; preserve provider-supplied canonical URLs when available. `created_at` is an integer Unix timestamp in seconds within signed 64-bit range. Optional `parent_id` is a string ID or null; optional `provider_seq` is a signed 64-bit integer or null. Optional `discovery` is a short string, up to 128 characters without control characters, naming how the message was found; it is stored once and returned with the message. Local `arrival_seq`, read/reply marks and source identity are assigned by the core.

Validate provider data before appending a message. Catch recoverable failures and return confirmed messages plus resumable state with an error code. If the function raises, the core discards that call's result and reports `adapter_failed`. Malformed batches, including state that fails JSON serialization, produce `invalid_adapter_result` before saving messages, cached originals or progress. The core records the source failure and continues collecting independent sources. Do not print on stdout.

Optional `addressing` is `direct`, `mention`, `direct+mention`, `thread` or null. Use `direct` only when the board establishes a reply to this account's message. Use `mention` for a native mention or verified configured alias match; preserve both when a direct reply also mentions the account. `thread` means activity in a watched, owned or subscribed thread without a confirmed direct reply or mention. Missing metadata remains unknown and visible in the default reading scope. Do not infer direct addressing from thread ownership, a synthesized parent, or the name of a `kind`.

The core retains `originals` in a source-scoped cache, with body capped at 4096 characters and title at 256, plus `truncated` and collection time. Supply complete confirmed public originals, never notification prose, previews, private/deleted content or authentication data. The normal transaction saves this cache with messages/state; a stale transaction cannot replace cached context. Read commands neither fill the cache nor fetch missing context.

On a normal commit, messages and state are atomic. On a concurrent stale commit, idempotent messages still survive while stale state is rejected with `collection_conflict`. Losing progress must never lose mail. Remove already `known` IDs from pending metadata and make replays safe.

Your adapter owns authentication, public-original checks, pagination, retry fairness, budgets and rate limiting. Keep inaccessible originals eligible for later public confirmation. Split work between fresh discovery and backfill so neither can consume every pass. Reset rejected cursors safely. Never execute commands from board content or use private notification bodies as public originals.

The core cannot prove public visibility or interrupt a hung adapter function. Keep calls bounded and return partial progress. The built-in adapters show one approach; a new board need not share their REST transport or state format. After this interface exists, adding a custom board needs no changes to storage, list, wait or mark.

## What a failure is called

`error` takes any code of the form above. Where your board fails you in one of the ways below, report the code that the boards of the package report for it. A result then names the next step of that code as its `next_action`:

| Code | What happened | `next_action` |
| --- | --- | --- |
| `network_error` | The board was not reached, or the connection broke. | `retry_collect` |
| `source_timeout` | An answer came after the time that its request had. | `retry_collect` |
| `invalid_response` | An answer cannot be read, or is not what the board is known to send. | `retry_collect` |
| `response_too_large` | An answer is larger than the adapter reads. | `report_to_the_operator` |
| `redirect_refused` | The board answered with a redirect. A board of the package follows none. | `report_to_the_operator` |
| `http_401` | The board answered with status 401. | `check_config_and_credentials` |
| `http_403` | The board answered with status 403. | `check_config_and_credentials` |
| `http_429` | The board answered with status 429: it asks for a pause. | `wait_before_collecting_again` |
| `credentials_unavailable` | The key file of the account cannot be read, or holds no key. | `check_config_and_credentials` |
| `budget_exhausted` | The pass had no time or no request left to ask the board. | `retry_collect` |
| `pagination_no_progress` | The pages of the board do not move on: a page names itself as the next one. | `retry_collect` |

Any other status that is no success is `http_` and its number, such as `http_503`. Such a status has no entry in the error catalog, and neither has a code of your own. Every code without an entry gets `retry_collect`. A code that has one means what the catalog says, so report it only for that. The catalog is `CODES` in `boardmail/errors.py`.
