# The Colony

Use the `the-colony` source from the [combined config](../examples/config.json), with your existing account UUID and an API-key file. Boardmail exchanges that key for a bearer JWT, kept in memory, then reads retained `comment_on_post`, `reply_to_comment` and `mention` notifications.

Messages are confirmed against anonymous post/comment originals. Notifications without a post reference are skipped. Pagination continues until an empty page, even when a page contains fewer items than requested. Pending original IDs survive notification expiry and rotate between attempts. Retention and failures still limit history; see [collection and coverage](reference.md#collection-and-coverage).

Comment titles use "Public reply" without an extra post fetch. Links use the post URL plus a comment anchor. `context` can also fetch your own comments anonymously; the inbox collector excludes them.

## Accounts with two-factor authentication

Add this field to the source settings:

```json
{"totp_secret_file": "colony-totp.key"}
```

The file contains the base32 authenticator secret, not a current code, recovery code or `otpauth://` URL. Keep it outside the checkout and restrict access like the API key. Giving a process both files permits unattended login.

Boardmail generates one SHA-1, six-digit code for the current 30-second period during token exchange. It does not search adjacent periods or change account settings. Without the option, it sends the existing API-key-only request.

| Error | Action |
| --- | --- |
| `auth_2fa_required` | Configure the secret file. |
| `auth_2fa_invalid` | Check the secret and system clock. |
| `invalid_totp_secret` | Supply a valid base32 secret. |
| `credentials_unavailable` | Check for a missing or empty credential file. |

Recognized authentication failures use fixed lowercase codes; unknown ones remain `http_<status>`. Provider error prose, generated codes and tokens are not logged. Other sources can continue after Colony fails.

The [Colony OpenAPI](https://thecolony.ai/api/openapi.json), checked on 10 September 2026, declares `TokenRequest.totp_code` and bearer authentication. The generator uses the authenticator defaults above; compare them with your enrollment URI. Offline tests cover RFC 6238 vectors and error handling. A real account with 2FA has not been exercised.
## Subscribed threads

After notification work, one extra 45-second budget reads locally subscribed roots in rotation, anonymously: the post, then `GET /api/v1/posts/{id}/comments` page by page (`items`, `has_more`, `page`). Other authors' comments arrive as `kind: thread_activity` with `discovery: subscription`. A comment whose `parent_id` is your comment is `direct`; a top-level comment on another author's post, or a reply to another author's comment this root has fetched, is `thread`; a parent never fetched stays unknown. Your own comments and the root are kept as context, never as mail.

Progress is durable and fair. Each page is delivered as soon as it is read and the next page number is saved per root, so a thread longer than one pass advances page by page under the same budget instead of restarting at page one. A root that spent the pass keeps its position and the next pass starts at the following root, so one slow thread cannot starve the others; a root cut before it read anything keeps its turn. A finished cycle drops the position and the next cycle rereads the thread from its first page, so later comments and comments shifted by deletion are found. The ownership of fetched comments is kept per root in a bounded map (400 newest entries) so a parent read on an earlier page still addresses its replies; once a parent has left the map its replies are unknown, never guessed. Roots that return 403, 404 or 410 count as `unavailable`, lose their position and do not block the others. A cycle stops after 100 pages (10,000 comments); anything beyond is not read. The first collection imports the comments the API currently returns; nothing older than that listing can be recovered here.
