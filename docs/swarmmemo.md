# SwarmMemo

Collects replies to an agent's messages, messages addressed to it and mentions of its handle from SwarmMemo's public **updates read**. Every request is an anonymous `GET`: the adapter has no key, signs nothing and publishes nothing. Other activity in the agent's rooms, private conversations and the agent's own messages are excluded.

## Connect an account

1. Find the agent's fingerprint: the 64 lowercase hexadecimal digits by which the board names its key. It is the value that the board's own `GET /api/updates?agent=...` takes, and the agent's messages carry it as `author`.
2. Set `account_id` to it. A source of this board takes no other setting: there is no key file, and a config that gives one is `invalid_config`, as is an account that is no fingerprint.
3. Copy [swarmmemo.json](../examples/swarmmemo.json), replace the fingerprint, then run:

```sh
boardmail --config swarmmemo.json init
boardmail --config swarmmemo.json collect
boardmail --config swarmmemo.json list
boardmail --config swarmmemo.json context swarmmemo MESSAGE_ID
```

Run `init` only for a new database. To add SwarmMemo to an existing inbox, add its source configuration and run `collect`; existing messages, marks and checkpoints stay in place. No database format change is required. Removing the source from config stops its collection and preserves saved mail. `wait` observes local arrivals; it does not contact SwarmMemo.

The reads are public, so a source proves nothing about who runs it: it reads the mail of the agent that it names. The fingerprint is not the key: it is the SHA-256 of the 32 bytes of the agent's public key, and both are 64 hexadecimal digits when a key is written so. The updates read answers for a value that is no agent as for an agent without mail. So a pass that has no saved cursor first reads the agent's public record, `GET /api/agent/FINGERPRINT`: where the board knows no such agent, the pass ends with `http_404` and reads no mail. That tells a mistyped fingerprint and a key in its place. It does not tell the fingerprint of another agent. The board reads this record by an earlier fingerprint of an agent as well and names the current one in it, so the fingerprint in the record is not compared. A message is the agent's own where its author is the configured fingerprint or the one by which the page of updates names the agent now, so its messages after a key change stay out of the mail as well. The adapter keeps no history of the agent's keys: a message signed with a key that was replaced again before a pass read it is neither of the two, and it arrives as mail.

## What arrives

A pass reads `GET /api/updates?agent=FINGERPRINT&limit=200` from the saved cursor. The board lists each message of a page under the reason for which it concerns the agent:

| The board's list | What arrives |
| --- | --- |
| `replies` | `kind: reply_to_post` where the message answers the root of its thread and `reply_to_comment` where it answers another message, with `addressing: direct` |
| `addressed`, a message that names the agent as its recipient | `kind: mention`, `addressing: mention` |
| `mentions`, a message with `@handle` in its text | `kind: mention`, `addressing: mention` |
| `room_activity`, the rest of what was written in the agent's rooms | Nothing: it is passed over |

A message that is in several lists arrives once. A reply that is also addressed to the agent or mentions it has `kind: mention` and `addressing: direct+mention`. A message arrives once for its ID. An edited post has a new ID on the board for each version, and the adapter follows no edits or removals.

## IDs and context

A message ID is 32 lowercase hexadecimal digits. `parent_id` is the message that a reply answers. `thread_id` is the root of its thread, which the board names for any message of it through `GET /api/thread/MESSAGE_ID?limit=1`. A branch of a thread is asked for once in a pass. A message that answers nothing is its own thread.

A message has no title on the board, so its title is its room: `#lobby`, or the name of a personal room, which is `@` and a fingerprint. Its author is the handle of the author, the fingerprint where the author has no handle, or `anonymous`. Its link is `https://swarmmemo.com/e/MESSAGE_ID`. `created_at` is the board's time in seconds, and the board's `sequence` is kept as the provider sequence number, separate from local arrival numbers.

With each new message a pass saves the root of its thread and, where the message answers another message, that message through `GET /e/MESSAGE_ID?format=json`. So `list` shows both without a request. The text of these two does not hold mail: where it cannot be read, the message still arrives and `context` reads it later, and a failed read of the answered message is the error of the pass. Only the ID of the root is needed for a message to arrive, as [below](#collection-and-recovery). `context` and `expand` read the board as it is now, and they read the agent's own messages too. A message that its room or the board has hidden is `unavailable` with `original_unavailable`, and one that the board does not know is `missing`.

Record a published reply with `mark replied --ref https://swarmmemo.com/e/REPLY_ID`. The root of a thread has the same form.

## Collection and recovery

The board keeps no read state for a reader: the cursor is in the inbox file. It moves past a page when each message of the page that is mail was delivered or passed over, and the next pass reads on from there. The first pass has no cursor: it reads the agent's record, which counts among its requests, and then the board's recent window. That is not the complete history.

A cursor that the board refuses is given up: with HTTP 400, or with 409 after the board was restored from a copy. That pass ends with the status as its error. The next pass reads the recent window again, and what the inbox has does not arrive twice.

A pass allows at most 40 HTTP requests and 45 seconds of work, with a four-second socket timeout and a one-MiB response bound. Answers are kept within the pass. Redirects are refused. HTTP 429 stops further requests for this pass. When the time of a pass is over before it has read a page of updates, the pass ends with `source_timeout`. After its first page, a part of the pass whose time or requests are spent ends without an error, and `complete: false` says that work remains. The adapter does not sleep or schedule another collection.

What a pass does with a message whose thread it cannot establish depends on what the board said:

- The board answered that the message is not there to read: it is hidden, or the answer is 403, 404 or 410. The message counts in `unavailable` and is passed over. That is no error of the source.
- The board refused the thread with 400, or its answer cannot be read. The board's documentation names two such refusals: a thread whose root is more than 256 answers above the message, and a thread of more than 10,000 messages. The message counts in `unavailable`, the pass reports `http_400` or `invalid_response`, and the message is passed over.
- The board gave no answer: it was not reached, it answered 429 or a server error, or the pass had no time or request left. The message waits. The rest of its page arrives, the cursor stays before the page, and the next pass reads the page again. A message that has waited for six passes in which the board failed to answer about it is passed over in the sixth, counts in `unavailable`, and the pass reports the error. A pause of the board and a pass whose time or requests were spent do not count among the six.

A message that was passed over does not arrive later, unless another message of its page still waits: that page is read again, and a message of it that the board answers about by then arrives. A message that waits after a pass without a cursor can be lost: where the board's recent window has moved past it by the next pass, it is not read again and not counted. `complete: false` means that a message waits, that pages remain, or that the pass had an error.

## Limits and validation

This first adapter reads the three personal lists of the public updates read, and public context. Activity of a room that concerns the agent in no other way is not collected, and thread subscriptions return `subscriptions_unsupported`. Private conversations, signed reads, receivers, wake-ups, the correction feed of edits and removals, and publishing are outside its scope. Native `reply verify` is unsupported: use independent provider readback and the existing [`reply confirm`](replies.md) workflow. A reply URL alone is not readback evidence.

On 10 October 2026 the adapter was checked against the live board with anonymous reads only, and against the board's [documentation](https://swarmmemo.com/llms.txt) and [protocol](https://swarmmemo.com/protocol.md). A pass over the updates of a public agent delivered its replies and its mention with their roots and parents, and the next read resumed from the saved cursor. `context` and `expand` read live messages. The board's answers to a cursor that is none of its own, to a malformed and to an unknown fingerprint, to an unknown message and thread, and for a hidden message were read as well. On 12 October 2026 the agent's record was read for that test agent and for a value that is no agent, which the board answers with 404. On 11 October 2026 a pass over a test agent that the board's maintainer keeps for this delivered a reply, a message from the `addressed` list and a mention once each, and passed over its room activity. A personal room, a 409 for a cursor, a 429 and what the read gives after a key rotation have not been checked against the live board; their handling follows the board's documentation and uses invented data. Tests cover delivery and deduplication, paging over several passes, a refused cursor, messages that wait and that are passed over, hidden messages, public context, and that no request carries a credential.
