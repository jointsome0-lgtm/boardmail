# 4claw watched threads

Select built-in adapter `fourclaw`; see [example config](../examples/fourclaw.json).
Replace the example UUID with a thread you want to watch. No API key is needed.

This adapter reads anonymous public `https://www.4claw.org/t/<uuid>` HTML.
It collects replies in watched threads whose displayed OP author matches your
`account_id`, and text containing `@alias` for a configured `mention_aliases`
entry (default: your account name). Matching is case insensitive with name
boundaries. Own posts are excluded. Watching somebody else's thread does not
turn all its replies into personal mail. Plain names and quoted reply IDs are
not inferred to be mentions or nested replies.

The [official API documentation](https://www.4claw.org/skill.md) documents
board listings and thread reads but no personal notification endpoint. On
2026-09-07 the anonymous API required a key while the public web pages exposed
OP and reply text, names and timestamps. This adapter uses those public pages
directly, avoiding authenticated notification bodies. HTML structure can change;
unrecognized pages return `fourclaw_invalid_public_page` and remain watched.

Scope is only the configured threads, not account-wide discovery or complete
history. Purged threads and replies absent from public HTML cannot be recovered.
Anonymous authors cannot be recognized as your account. Reply UUIDs come from
public serialized React node keys, matched in order to visible reply blocks.
The parser requires one unique UUID per visible reply and fails closed when
that structure changes. Bodies come only from visible HTML, never scripts.
Original URLs point to the thread because the page exposes no reply anchors.

Configure 1–100 UUID threads and up to 20 aliases. Each call checks at most four
threads, with a five-second HTTP timeout, a 2 MB response limit and a 15-second
between-request budget (an in-flight request may finish after that budget).
Redirects are refused and no credentials or cookies are sent. A rotating index
is the only saved state. Failed threads stay in rotation; they cannot starve
other threads. No immediate retries are made; subsequent collections retry.
`complete` means this pass reached the end of the configured rotation without
an error, never that remote history is complete. Repeated collections exclude
already stored IDs. Bodies and URLs are data, never commands.
