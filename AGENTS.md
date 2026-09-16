# Panopticon

A single-file Python script that reads meeting-transcript notification emails from a
Gmail label, follows the linked Google Doc, extracts a structured meeting summary, and
posts it to a chat endpoint.

Read `README.md` for the document format, configuration variables, and scheduling. This
file covers only what an agent needs to work in the repo.

## Layout

- `transcript_sync.py` — the whole program. Executable, with a PEP 723 inline
  dependency header.
- `test_transcript_sync.py` — all tests. Also executable with its own inline header.
- `ruff.toml`, `.pre-commit-config.yaml` — lint, format, and type-check configuration.

There is no package, no `pyproject.toml`, and no test runner configuration. Keep it that
way unless the script outgrows one file.

## Data flow

1. Connect to Gmail over IMAP with an app password. Select the label given on the
   command line.
2. Find the first Google Docs link in the message body.
3. Read the document through the Google Docs API, using a cached OAuth token.
4. Parse the document into a strict payload: title, date, invitees, and the `Summary`,
   `Decisions`, and `Next Steps` sections.
5. Render the payload as chat content and POST it to `STAVROBOT_BASE_URL` + `/chat`.
6. Mark the message read. Any failure before this point leaves the message unread, so
   the next run retries it.

Commands: `authorize`, `preview`, `sync` (with optional `--dry-run`).

## Commands

```sh
uv run --script test_transcript_sync.py   # tests
pre-commit run --all-files                # lint, format, mypy, pyupgrade
```

Run `pre-commit` twice after a change: the first pass fixes what it can.

The tests mock Gmail, Google Docs, and the endpoint. They never touch the network.

## Conventions

- Every function signature is fully typed. Use built-in generics (`list`, `dict`), not
  the `typing` equivalents.
- Imports go at the top of the file, one name per line. Ruff's isort rules enforce this.
- No defensive `try`/`except`. Let exceptions propagate. The retry behaviour depends on
  failures being loud.
- Full words, not abbreviations: `message_identifier`, not `msg_id`.
- Parsing is deliberately strict. A document that does not match the expected shape is
  rejected, not guessed at. Do not add fallbacks.

## Credentials

Never read `.envrc`, `.env*`, or any token cache. They contain live secrets. The OAuth
token cache lives outside the checkout, under
`${XDG_CONFIG_HOME:-$HOME/.config}/panopticon/`.

Never put real addresses, tokens, or URLs in tickets, commits, or documentation. Use
placeholders.
