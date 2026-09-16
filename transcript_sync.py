#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "google-api-python-client",
#   "google-auth-oauthlib",
# ]
# ///
# Export GMAIL_ADDRESS and GMAIL_APP_PASSWORD before preview or sync. Export
# GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET before authorize; the
# token cache carries its own copy of both, so refreshes do not need them.
# Only non-dry-run sync requires STAVROBOT_BASE_URL; dry runs need no endpoint
# configuration. Optionally set GOOGLE_OAUTH_TOKEN_CACHE_PATH and
# TRANSCRIPT_ENDPOINT_TOKEN.
import argparse
import base64
import email
import html
import imaplib
import json
import os
import re
import ssl
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import cast
from urllib.parse import unquote
from urllib.parse import urlsplit
from urllib.request import build_opener
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

DOCUMENTS_SCOPE = "https://www.googleapis.com/auth/documents.readonly"
DOCUMENT_LINK_PATTERN = re.compile(
    r"https?://docs\.google\.com/document/d/([A-Za-z0-9_-]+)"
)
SECTION_KEYS = {
    "summary": "summary",
    "decisions": "decisions",
    "next steps": "next_steps",
    "details": "details",
}
# Chat label and render order for each section body, because SECTION_KEYS keys
# are lowercased and cannot supply the source capitalization.
SECTION_LABELS = (
    ("Summary", "summary"),
    ("Decisions", "decisions"),
    ("Next steps", "next_steps"),
    ("Details", "details"),
)
# Gemini's generated notes close the Details narrative with a footer asking the
# reader to review the notes. Formatting cannot reliably mark that boundary,
# because italic or gray text also occurs in legitimate content, so matching the
# literal wording avoids dropping real paragraphs. If Gemini rewords the footer,
# its text flows into Details until this marker is updated.
DETAILS_FOOTER_MARKER = "You should review Gemini's notes"
TranscriptPayload = dict[str, str | list[dict[str, str]]]
ChatRequest = dict[str, str | bool]
CHAT_INTRODUCTION = "Here is the data that was parsed from a recent meeting:"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        status_code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        # Replaying a POST at a redirect target can send transcripts to an
        # unconfigured host, so redirects are failures instead of follow-ups.
        return None


def connect_mailbox() -> imaplib.IMAP4_SSL:
    mailbox = imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993,
        ssl_context=ssl.create_default_context(),
    )
    mailbox.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
    return mailbox


def select_mailbox(
    mailbox: imaplib.IMAP4_SSL, folder_name: str, readonly: bool
) -> None:
    response_status, _ = mailbox.select(folder_name, readonly=readonly)
    if response_status != "OK":
        raise RuntimeError("Unable to select mailbox folder")


def search_messages(mailbox: imaplib.IMAP4_SSL, search_criterion: str) -> list[bytes]:
    response_status, message_response = mailbox.search(None, search_criterion)
    if response_status != "OK":
        raise RuntimeError("Unable to search mailbox folder")
    return message_response[0].split()


def sorted_message_identifiers(message_identifiers: list[bytes]) -> list[bytes]:
    return sorted(message_identifiers, key=int)


def fetch_message(mailbox: imaplib.IMAP4_SSL, message_identifier: bytes) -> Message:
    # Fetching without BODY.PEEK causes Gmail to set \Seen before the payload
    # has reached the endpoint, breaking the mail flag's retry guarantee.
    # imaplib's stub is stricter than the runtime, which accepts byte identifiers.
    response_status, fetched_message = mailbox.fetch(
        message_identifier,  # type: ignore[arg-type]
        "(BODY.PEEK[])",
    )
    if response_status != "OK":
        raise RuntimeError("Unable to fetch message")

    for response_part in fetched_message:
        if isinstance(response_part, tuple):
            return email.message_from_bytes(response_part[1])
    raise RuntimeError("Fetched message contains no content")


def mark_message_seen(mailbox: imaplib.IMAP4_SSL, message_identifier: bytes) -> None:
    # imaplib's stub is stricter than the runtime, which accepts byte identifiers.
    response_status, _ = mailbox.store(
        message_identifier,  # type: ignore[arg-type]
        "+FLAGS",
        r"(\Seen)",
    )
    if response_status != "OK":
        raise RuntimeError("Unable to mark message as read")


def first_document_identifier(message: Message) -> str:
    # The document link appears only in the text/html alternative of these
    # mails, not in the text/plain one, so every text part is searched.
    # Entities are unescaped first because an href in HTML encodes & as &amp;.
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        assert isinstance(payload, bytes)
        part_text = html.unescape(payload.decode(part.get_content_charset() or "utf-8"))
        document_match = DOCUMENT_LINK_PATTERN.search(part_text)
        if document_match is not None:
            return document_match.group(1)

    raise RuntimeError("No linked transcript document found")


def oauth_token_cache_path() -> Path:
    default_config_directory = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    return Path(
        os.environ.get(
            "GOOGLE_OAUTH_TOKEN_CACHE_PATH",
            default_config_directory / "panopticon" / "google_oauth_token.json",
        )
    )


def authenticate(allow_interactive: bool = False) -> Credentials:
    token_cache_path = oauth_token_cache_path()
    credentials: Credentials | None = None
    if token_cache_path.exists():
        credentials = Credentials.from_authorized_user_file(
            token_cache_path, [DOCUMENTS_SCOPE]
        )

    if credentials is None or not credentials.valid:
        if (
            credentials is not None
            and credentials.expired
            and credentials.refresh_token
        ):
            credentials.refresh(GoogleRequest())
        else:
            if not allow_interactive:
                raise RuntimeError(
                    f"Google OAuth token cache is missing or unusable at {token_cache_path}; "
                    "run `transcript_sync.py authorize` before running preview or sync"
                )
            authorization_flow = InstalledAppFlow.from_client_config(
                {
                    "installed": {
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                        "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                        "redirect_uris": ["http://localhost"],
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                },
                [DOCUMENTS_SCOPE],
            )
            credentials = authorization_flow.run_local_server()
        token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        token_cache_descriptor = os.open(
            token_cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        os.fchmod(token_cache_descriptor, 0o600)
        with os.fdopen(token_cache_descriptor, "w", encoding="utf-8") as token_cache:
            token_cache.write(credentials.to_json())

    return credentials


def authorize() -> None:
    token_cache_path = oauth_token_cache_path()
    authenticate(allow_interactive=True)
    print(token_cache_path)


def create_documents_service() -> Resource:
    return build("docs", "v1", credentials=authenticate())


def fetch_document(
    documents_service: Resource, document_identifier: str
) -> dict[str, object]:
    try:
        return cast(
            dict[str, object],
            documents_service.documents()
            .get(documentId=document_identifier, includeTabsContent=True)
            .execute(),
        )
    except HttpError as error:
        # Google embeds document-specific data in error bodies, so a status is
        # the only safe diagnostic for the unattended runner to propagate.
        raise RuntimeError(
            f"Google Docs request failed with HTTP status {error.resp.status}"
        ) from None


def paragraph_text(paragraph: dict[str, object]) -> str:
    text_fragments: list[str] = []
    paragraph_elements = cast(list[dict[str, object]], paragraph["elements"])
    for paragraph_element in paragraph_elements:
        if "textRun" not in paragraph_element:
            continue
        text_run = cast(dict[str, object], paragraph_element["textRun"])
        text_fragments.append(cast(str, text_run["content"]))
    return "".join(text_fragments).replace("\x0b", "\n")


def normalized_label_text(label_text: str) -> str:
    return (
        " ".join(label_text.replace("\xa0", " ").replace("\x0b", " ").split())
        .rstrip(":.-")
        .strip()
    )


def document_tabs(document: dict[str, object]) -> list[dict[str, object]]:
    tabs: list[dict[str, object]] = []

    def add_tab_and_child_tabs(tab: dict[str, object]) -> None:
        tabs.append(tab)
        child_tabs = cast(list[dict[str, object]], tab.get("childTabs", []))
        for child_tab in child_tabs:
            add_tab_and_child_tabs(child_tab)

    top_level_tabs = cast(list[dict[str, object]], document["tabs"])
    for top_level_tab in top_level_tabs:
        add_tab_and_child_tabs(top_level_tab)
    return tabs


def tab_paragraphs(tab: dict[str, object]) -> list[dict[str, object]]:
    document_tab = cast(dict[str, object], tab["documentTab"])
    body = cast(dict[str, object], document_tab["body"])
    structural_elements = cast(list[dict[str, object]], body["content"])
    paragraphs: list[dict[str, object]] = []
    for structural_element in structural_elements:
        if "paragraph" in structural_element:
            paragraphs.append(cast(dict[str, object], structural_element["paragraph"]))
    return paragraphs


def heading_level(paragraph: dict[str, object]) -> int | None:
    paragraph_style = cast(dict[str, object], paragraph["paragraphStyle"])
    named_style_type = cast(str, paragraph_style["namedStyleType"])
    if not named_style_type.startswith("HEADING_"):
        return None
    return int(named_style_type.removeprefix("HEADING_"))


def section_text(
    paragraphs: list[dict[str, object]],
    heading_index: int,
    section_heading_level: int,
    stop_marker: str | None = None,
) -> str:
    section_paragraphs: list[str] = []
    for paragraph in paragraphs[heading_index + 1 :]:
        next_heading_level = heading_level(paragraph)
        if (
            next_heading_level is not None
            and next_heading_level <= section_heading_level
        ):
            break

        text = paragraph_text(paragraph).strip()
        if not text:
            continue
        if stop_marker is not None and text.startswith(stop_marker):
            break
        if "bullet" in paragraph:
            text = f"- {text}"
        section_paragraphs.append(text)
    return "\n".join(section_paragraphs)


def required_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Missing or invalid {field_name}")
    return cast(dict[str, object], value)


def required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Missing or invalid {field_name}")
    return value


def extract_date(paragraphs: list[dict[str, object]]) -> str:
    date_elements: list[dict[str, object]] = []
    for paragraph in paragraphs:
        paragraph_elements = cast(list[dict[str, object]], paragraph["elements"])
        for paragraph_element in paragraph_elements:
            if "dateElement" in paragraph_element:
                date_elements.append(
                    required_mapping(paragraph_element["dateElement"], "date element")
                )

    if len(date_elements) != 1:
        raise RuntimeError("Expected exactly one date element")

    date_properties = required_mapping(
        date_elements[0].get("dateElementProperties"), "date element properties"
    )
    timestamp = required_string(date_properties.get("timestamp"), "date timestamp")
    # The Docs API specifies UTC when timeZoneId is omitted, so rejecting it
    # would reject the standard representation returned by these documents.
    time_zone_identifier = required_string(
        date_properties.get("timeZoneId", "Etc/UTC"), "date time zone"
    )
    try:
        parsed_timestamp = datetime.fromisoformat(
            f"{timestamp.removesuffix('Z')}+00:00"
            if timestamp.endswith("Z")
            else timestamp
        )
    except ValueError:
        raise RuntimeError("Invalid date timestamp") from None
    if parsed_timestamp.tzinfo is None:
        raise RuntimeError("Date timestamp must include a time zone")
    try:
        time_zone = ZoneInfo(time_zone_identifier)
    except (ValueError, ZoneInfoNotFoundError):
        raise RuntimeError("Invalid date time zone") from None
    return parsed_timestamp.astimezone(time_zone).date().isoformat()


def extract_invitees(paragraphs: list[dict[str, object]]) -> list[dict[str, str]]:
    person_chip_paragraphs: list[list[dict[str, object]]] = []
    for paragraph in paragraphs:
        person_chips: list[dict[str, object]] = []
        paragraph_elements = cast(list[dict[str, object]], paragraph["elements"])
        for paragraph_element in paragraph_elements:
            if "person" in paragraph_element:
                person_chips.append(
                    required_mapping(paragraph_element["person"], "person chip")
                )
        if person_chips:
            person_chip_paragraphs.append(person_chips)

    if not person_chip_paragraphs:
        # Ad-hoc meetings may legitimately have no invitees, so a Summary tab
        # with zero person-chip paragraphs yields an empty list. Narrative text
        # is never scanned for names, because that would infer attendance.
        return []

    if len(person_chip_paragraphs) != 1:
        raise RuntimeError("Expected person chips in exactly one paragraph")

    person_chip_paragraph = next(
        paragraph
        for paragraph in paragraphs
        if any(
            "person" in element
            for element in cast(list[dict[str, object]], paragraph["elements"])
        )
    )
    paragraph_style = required_mapping(
        person_chip_paragraph.get("paragraphStyle"), "person chip paragraph style"
    )
    if (
        paragraph_style.get("namedStyleType") != "NORMAL_TEXT"
        or "bullet" in person_chip_paragraph
    ):
        raise RuntimeError(
            "Person chips must be in a non-bulleted normal-text paragraph"
        )

    invitees: list[dict[str, str]] = []
    for person_chip in person_chip_paragraphs[0]:
        person_properties = required_mapping(
            person_chip.get("personProperties"), "person properties"
        )
        invitees.append(
            {
                "name": required_string(person_properties.get("name"), "person name"),
                "email": required_string(
                    person_properties.get("email"), "person email"
                ),
            }
        )
    return invitees


def extract_sections(document: dict[str, object]) -> TranscriptPayload:
    summary_tabs: list[dict[str, object]] = []
    for tab in document_tabs(document):
        for paragraph in tab_paragraphs(tab):
            if (
                heading_level(paragraph) is not None
                and normalized_label_text(paragraph_text(paragraph)).casefold()
                == "summary"
            ):
                summary_tabs.append(tab)
                break

    if len(summary_tabs) != 1:
        raise RuntimeError("Expected exactly one tab with a Summary heading")

    summary_tab_paragraphs = tab_paragraphs(summary_tabs[0])
    section_heading_indices: dict[str, list[tuple[int, int]]] = {
        section_key: [] for section_key in SECTION_KEYS.values()
    }
    for paragraph_index, paragraph in enumerate(summary_tab_paragraphs):
        section_heading_level = heading_level(paragraph)
        if section_heading_level is None:
            continue
        section_key = SECTION_KEYS.get(
            normalized_label_text(paragraph_text(paragraph)).casefold()
        )
        if section_key is None:
            continue
        section_heading_indices[section_key].append(
            (paragraph_index, section_heading_level)
        )

    for section_key, heading_indices in section_heading_indices.items():
        if len(heading_indices) > 1:
            raise RuntimeError(f"Expected at most one {section_key} heading")

    sections: TranscriptPayload = {
        "title": required_string(document.get("title"), "document title"),
        "date": extract_date(summary_tab_paragraphs),
        "invitees": extract_invitees(summary_tab_paragraphs),
    }
    for section_key, heading_indices in section_heading_indices.items():
        if not heading_indices:
            sections[section_key] = ""
            continue
        paragraph_index, section_heading_level = heading_indices[0]
        stop_marker = DETAILS_FOOTER_MARKER if section_key == "details" else None
        sections[section_key] = section_text(
            summary_tab_paragraphs,
            paragraph_index,
            section_heading_level,
            stop_marker=stop_marker,
        )
    return sections


def chat_request(payload: TranscriptPayload) -> ChatRequest:
    invitees = cast(list[dict[str, str]], payload["invitees"])
    metadata_lines = [
        f"Title: {payload['title']}",
        f"Date: {payload['date']}",
    ]
    # Ad-hoc meetings have no invitees, so the block is omitted entirely rather
    # than sending a bare "Invitees:" label with nothing under it.
    if invitees:
        metadata_lines.append("Invitees:")
        metadata_lines.extend(
            f"- {invitee['name']} <{invitee['email']}>" for invitee in invitees
        )
    message_parts = [CHAT_INTRODUCTION, "\n".join(metadata_lines)]
    for label, section_key in SECTION_LABELS:
        section_body = payload[section_key]
        if section_body:
            message_parts.append(f"{label}:\n{section_body}")
    return {
        "message": "\n\n".join(message_parts),
        "source": "panopticon",
        "sender": "panopticon",
        "async": True,
    }


def post_payload(payload: ChatRequest) -> None:
    request_headers = {"Content-Type": "application/json"}
    endpoint = urlsplit(os.environ["STAVROBOT_BASE_URL"])
    endpoint_token = os.environ.get("TRANSCRIPT_ENDPOINT_TOKEN")
    if endpoint.username is not None:
        if endpoint_token is not None:
            raise ValueError(
                "Configure either Basic authentication or a bearer token, not both"
            )
        credentials = f"{unquote(endpoint.username)}:{unquote(endpoint.password or '')}"
        request_headers["Authorization"] = "Basic " + base64.b64encode(
            credentials.encode("utf-8")
        ).decode("ascii")
        # Keep credentials out of the transport URL and its error messages.
        endpoint = endpoint._replace(netloc=endpoint.netloc.rsplit("@", 1)[1])
    if endpoint_token is not None:
        request_headers["Authorization"] = f"Bearer {endpoint_token}"
    request = Request(
        f"{endpoint.geturl().rstrip('/')}/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with build_opener(NoRedirect()).open(request, timeout=120) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError("Transcript endpoint returned a non-success status")


def preview(folder_name: str) -> None:
    mailbox = connect_mailbox()
    select_mailbox(mailbox, folder_name, readonly=True)
    message_identifiers = sorted_message_identifiers(search_messages(mailbox, "ALL"))
    if not message_identifiers:
        raise RuntimeError("Mailbox folder contains no messages")
    message = fetch_message(mailbox, message_identifiers[-1])
    document_identifier = first_document_identifier(message)
    mailbox.logout()

    documents_service = create_documents_service()
    document = fetch_document(documents_service, document_identifier)
    print(json.dumps(extract_sections(document)))


def synchronize(folder_name: str, dry_run: bool = False) -> None:
    mailbox = connect_mailbox()
    select_mailbox(mailbox, folder_name, readonly=dry_run)
    message_identifiers = sorted_message_identifiers(search_messages(mailbox, "UNSEEN"))
    # Authorizing only when there is mail keeps an empty run from needing the
    # Google credentials at all.
    if message_identifiers:
        documents_service = create_documents_service()
        for message_number, message_identifier in enumerate(message_identifiers):
            message = fetch_message(mailbox, message_identifier)
            document_identifier = first_document_identifier(message)
            document = fetch_document(documents_service, document_identifier)
            print(
                f"Processing {document.get('title')!r} ({document_identifier})",
                flush=True,
            )
            extracted_sections = extract_sections(document)
            request_payload = chat_request(extracted_sections)
            if dry_run:
                if message_number:
                    print("---")
                print(request_payload["message"])
            else:
                print(
                    f"Processing {extracted_sections['date']}: "
                    f"{extracted_sections['title']}",
                    flush=True,
                )
                post_payload(request_payload)
                mark_message_seen(mailbox, message_identifier)
    mailbox.logout()
    # Printed on every path that completes, so its absence in the log means the
    # run died rather than finished with nothing to do.
    print("Done.")


def command_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or synchronize transcript documents from a mailbox folder."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview_parser = subparsers.add_parser("preview")
    preview_parser.add_argument("folder")
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("folder")
    subparsers.add_parser("authorize")
    return parser.parse_args()


def main() -> None:
    arguments = command_arguments()
    if arguments.command == "preview":
        preview(arguments.folder)
    elif arguments.command == "sync":
        synchronize(arguments.folder, dry_run=arguments.dry_run)
    elif arguments.command == "authorize":
        authorize()


if __name__ == "__main__":
    main()
