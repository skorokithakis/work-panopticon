# Panopticon

Panopticon reads meeting-transcript notifications from a Gmail label, follows the
linked Google Docs, extracts a strict meeting summary, and sends it to a chat
endpoint. It is a standalone Python script run with [uv](https://docs.astral.sh/uv/).

## Supported document format

Each Gmail message must contain a link to a Google Doc. Panopticon reads all tabs,
including nested tabs, but accepts a document only when exactly one tab contains a
`Summary` heading. In that tab it requires:

- A non-empty document title.
- Exactly one Google Docs date element, which becomes the meeting date.
- Exactly one non-bulleted, normal-text paragraph containing the invitees as Google
  Docs person chips. Every chip must have a name and email address.
- Exactly one heading each named `Summary`, `Decisions`, and `Next Steps`.

Headings are matched case-insensitively and tolerate trailing punctuation. Content
under a section continues until the next heading at the same or higher level.
Sections may be empty. Documents that do not meet this format are rejected rather
than guessed at.

## Requirements and Google setup

- [uv](https://docs.astral.sh/uv/).
- A Gmail account and a Gmail app password for IMAP access. The app password is
  used only to read Gmail; it does not authenticate Google Docs access.
- A Google Cloud project with the Google Docs API enabled and OAuth client
  credentials for the installed-app flow. Set the client ID and client secret
  below.

Run the `authorize` command interactively on a machine with a browser, then sign in
with a Google account that can access the linked documents. This is the only command
that opens a local browser-based OAuth flow. The script requests the
`https://www.googleapis.com/auth/documents.readonly` scope, which can read all
Google Docs accessible to that signed-in account; it is not limited to documents
linked by Gmail.

External OAuth apps left in Testing issue refresh tokens that expire after seven
days. Use an eligible Internal or Production OAuth configuration before relying
on the token cache for unattended scheduling.

After authorization, the credentials are cached at:

```
${XDG_CONFIG_HOME:-$HOME/.config}/panopticon/google_oauth_token.json
```

Set `GOOGLE_OAUTH_TOKEN_CACHE_PATH` to use another location. Keep the cache outside
the checkout: it contains a refresh token, and an untracked file in the repository
can be removed by `git reset --hard` followed by `git clean`.

## Configuration

Set these environment variables before running the script:

| Variable | Required for | Description |
| --- | --- | --- |
| `GMAIL_ADDRESS` | `preview` and `sync` | Gmail address used for IMAP login. |
| `GMAIL_APP_PASSWORD` | `preview` and `sync` | Gmail app password used for IMAP login. |
| `GOOGLE_OAUTH_CLIENT_ID` | `authorize` | OAuth client ID. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | `authorize` | OAuth client secret. |
| `GOOGLE_OAUTH_TOKEN_CACHE_PATH` | Optional | Overrides the token-cache path shown above. |
| `STAVROBOT_BASE_URL` | `sync` only | Base URL; Panopticon posts to its `/chat` path. |
| `TRANSCRIPT_ENDPOINT_TOKEN` | Optional | Bearer token for the chat endpoint. |

The endpoint supports one authentication method at a time:

- Put HTTP Basic credentials in `STAVROBOT_BASE_URL`, for example
  `https://<username>:<password>@<host>/<path>`. Percent-encode reserved
  characters in both the username and password.
- Or set `TRANSCRIPT_ENDPOINT_TOKEN` to send bearer authentication.

Do not set both URL userinfo and `TRANSCRIPT_ENDPOINT_TOKEN`; the script rejects
that configuration.

`STAVROBOT_BASE_URL` may use HTTP for a local deployment.

## Running

Replace `<gmail-label>` with the Gmail label or folder to read.

```sh
uv run --script transcript_sync.py authorize
uv run --script transcript_sync.py preview '<gmail-label>'
uv run --script transcript_sync.py sync --dry-run '<gmail-label>'
uv run --script transcript_sync.py sync '<gmail-label>'
```

`authorize` takes no folder argument and prints the token-cache path. It opens the
browser-based OAuth flow only when the cache is missing or unusable, so it is safe to
repeat. Run it before `preview` or `sync`; those commands refuse interactive
authorization and fail instead.

The token cache holds its own copy of the client ID and client secret, so refreshes
need no OAuth variables. Only `authorize` reads them. Unattended runs need neither.

`preview` reads the newest message in the label and prints extracted document JSON.
It does not post data or change mailbox flags.

`sync --dry-run` reads every unread message and prints the exact rendered chat
content for each one, separated by `---`. It does not post data or change mailbox
flags, and does not require endpoint configuration.

`sync` reads unread messages oldest first, logs `Processing <date>: <title>` before
each submission, posts the rendered content, then marks that message as read. No
`--push` flag is needed.

Both `sync` forms print `Done.` when they finish, including when there was nothing
unread. A log that ends without it means the run died partway.

The endpoint timeout is fixed at 120 seconds. If document extraction, submission,
or marking a message read fails, that message remains unread. A timeout or a failure
after the endpoint receives the request can therefore cause a later run to submit a
duplicate; the receiver should tolerate duplicates.

Run only one non-dry-run sync for a label at a time: concurrent runs can select
the same unread messages before either marks them as read. The script uses IMAP
message sequence numbers, so do not change the selected label while a sync is
running; mailbox changes can alter those sequence numbers.

## Scheduling

### Local hourly cron

Run `transcript_sync.py authorize` interactively first with the persistent token
cache outside the checkout that cron will use. Cron does not load shell startup files
or `.envrc`, so provide the variables through a secure wrapper or the cron environment.

This is a template for `crontab -e`; replace every placeholder and keep real values
out of the repository:

```
0 * * * * GMAIL_ADDRESS='<gmail-address>' GMAIL_APP_PASSWORD='<gmail-app-password>' GOOGLE_OAUTH_TOKEN_CACHE_PATH='<persistent-cache-path>' STAVROBOT_BASE_URL='<endpoint-base-url>' TRANSCRIPT_ENDPOINT_TOKEN='<bearer-token-or-omit>' <repository-path>/transcript_sync.py sync '<gmail-label>' >> <log-path> 2>&1
```

When using HTTP Basic authentication in the endpoint URL, omit
`TRANSCRIPT_ENDPOINT_TOKEN`.

### Optional `run-repo-script` deployment

If a deployment service provides a `run-repo-script`-style scheduled repository
runner, configure it with an hourly `0 * * * *` schedule and this command:

```
./transcript_sync.py sync '<gmail-label>'
```

Store the required environment variables in that service's secret store. Give the
runner a persistent cache location outside its checkout, such as
`<persistent-cache-path>`, and run `transcript_sync.py authorize` interactively with
that cache before scheduling unattended runs. A push flag is not needed: the runner
only needs to execute the checked-out script.

## Tests

The synthetic tests mock Gmail, Google Docs, and the endpoint; they do not contact a
mailbox or send a POST request.

```sh
uv run --script test_transcript_sync.py
uvx ruff check transcript_sync.py test_transcript_sync.py
```
