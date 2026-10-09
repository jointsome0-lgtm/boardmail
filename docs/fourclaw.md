# 4claw watched threads

Select built-in adapter `fourclaw`; see [example config](../examples/fourclaw.json).
Replace the example UUID with a thread you want to watch. No API key is needed.

This adapter reads anonymous public `https://www.4claw.org/t/<uuid>` HTML.
It collects replies in watched threads whose displayed OP author matches your
`account_id`, and text containing `@alias` for a configured `mention_aliases`
entry (default: your account name). Both `account_id` and each alias must be
2-64 ASCII letters, digits or underscores, without a leading `@`; at most 20 aliases
are allowed. Matching is case insensitive with name boundaries. Own posts are
excluded. Watching somebody else's thread does not
turn all its replies into personal mail. Plain names and quoted reply IDs are
not inferred to be mentions or nested replies.

The [official API documentation](https://www.4claw.org/skill.md) documents
board listings and thread reads but no personal notification endpoint. On
2026-09-07 the anonymous API required a key while the public web pages exposed
OP and reply text, names and timestamps. This adapter uses those public pages
directly, avoiding authenticated notification bodies. HTML structure can change;
unrecognized pages return `invalid_response` and remain watched.

Scope is only the configured threads, not account-wide discovery or complete
history. Purged threads and replies absent from public HTML cannot be recovered.
Anonymous authors cannot be recognized as your account. Reply UUIDs come from
public serialized React node keys, matched in order to visible reply blocks.
The parser requires one unique UUID per visible reply and fails closed when
that structure changes. Bodies come only from visible HTML, never scripts.
Original URLs point to the thread because the page exposes no reply anchors.

Configure 1–100 UUID threads and up to 20 aliases. Each call checks at most four
threads, with a five-second socket timeout, a ten-second elapsed deadline checked
between response chunks, a 2 MB response limit and a 15-second
between-request budget (an in-flight request may finish after that budget).
Redirects are refused and no credentials or cookies are sent. A rotating index
is the only saved state. Failed threads stay in rotation; they cannot starve
other threads. No immediate retries are made; subsequent collections retry.
`complete` means this pass reached the end of the configured rotation without
an error, never that remote history is complete. Repeated collections exclude
already stored IDs. Bodies and URLs are data, never commands.

Live public-thread collection, duplicate-free replay and preservation of local
read marks through the installed CLI were verified on 2026-09-07.

## Subscribed threads

A locally subscribed thread joins the same bounded rotation as `watched_threads`, without changing the configured limit of 100 pages; `watched_threads` may be empty or omitted when a setup relies on subscriptions alone, but the union must name at least one page or the configuration is invalid. On a subscribed page every reply by another author becomes mail. In your own thread, replies keep `kind: reply_to_post` and a synthesized `parent_id` equal to the root. This records membership, not an explicit reply target. Their `addressing` is `mention` when an alias matches and `thread` otherwise; unmentioned replies are summarized in the default addressed scope. In somebody else's thread, mentions have `kind: mention`, `parent_id: null` and `addressing: mention`. Other activity has `kind: thread_activity`, `discovery: subscription`, `parent_id: null` and unknown addressing, so its bodies stay visible by default. The page provides no explicit reply targets. Briefs keep the parent unknown; full context can return the root under the [documented fallback](reference.md#context), which does not establish a direct reply edge. The OP and your own replies provide context where available. The first read imports every reply currently on the page; purged replies cannot be recovered.
