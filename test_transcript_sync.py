#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "google-api-python-client",
#   "google-auth-oauthlib",
#   "httplib2",
# ]
# ///

import copy
import imaplib
import json
import os
import sys
import traceback
import unittest
from contextlib import ExitStack
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from typing import cast
from unittest.mock import Mock
from unittest.mock import patch
from urllib.request import Request

import httplib2
from googleapiclient.errors import HttpError

import transcript_sync


def paragraph(
    text: str, named_style_type: str, is_bulleted: bool = False
) -> dict[str, object]:
    result: dict[str, object] = {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": named_style_type},
            "elements": [{"textRun": {"content": text}}],
        }
    }
    if is_bulleted:
        result_paragraph = result["paragraph"]
        assert isinstance(result_paragraph, dict)
        result_paragraph["bullet"] = {}
    return result


def tab(content: list[dict[str, object]]) -> dict[str, object]:
    return {"documentTab": {"body": {"content": content}}}


def date_element(timestamp: str, time_zone_identifier: str) -> dict[str, object]:
    return {
        "dateElement": {
            "dateElementProperties": {
                "timestamp": timestamp,
                "timeZoneId": time_zone_identifier,
            }
        }
    }


def person_element(name: str, email_address: str) -> dict[str, object]:
    return {"person": {"personProperties": {"name": name, "email": email_address}}}


def example_document() -> dict[str, Any]:
    return {
        "title": "Example meeting",
        "tabs": [
            {
                **tab(
                    [
                        paragraph("Meeting notes\n", "TITLE"),
                        paragraph("The summary is in a child tab\n", "NORMAL_TEXT"),
                    ]
                ),
                "childTabs": [
                    tab(
                        [
                            {
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    "elements": [
                                        date_element(
                                            "2026-01-02T00:30:00Z",
                                            "America/Los_Angeles",
                                        )
                                    ],
                                }
                            },
                            {
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    "elements": [
                                        {"textRun": {"content": "Invitees\n"}},
                                        person_element(
                                            "Example attendee", "attendee@example.test"
                                        ),
                                    ],
                                }
                            },
                            paragraph("Summary\n", "HEADING_3"),
                            paragraph("Concise overview\n", "NORMAL_TEXT"),
                            paragraph("First point\n", "NORMAL_TEXT", is_bulleted=True),
                            paragraph("Decisions\n", "HEADING_3"),
                            paragraph("Approve the proposal\n", "NORMAL_TEXT"),
                            paragraph("Next\xa0Steps:\n", "HEADING_3"),
                            paragraph(
                                "Send follow-up\n", "NORMAL_TEXT", is_bulleted=True
                            ),
                            paragraph("Details\n", "HEADING_3"),
                            paragraph("This must not be output\n", "NORMAL_TEXT"),
                        ]
                    )
                ],
            },
            tab(
                [
                    paragraph("Raw transcript\n", "TITLE"),
                    paragraph("Decoy transcript text\n", "NORMAL_TEXT"),
                ]
            ),
        ],
    }


class FetchingMailbox:
    def __init__(self) -> None:
        self.fetch_query: str | None = None

    def fetch(
        self, message_identifier: bytes, query: str
    ) -> tuple[str, list[tuple[bytes, bytes]]]:
        self.fetch_query = query
        return "OK", [(b"1", b"From: sender@example.test\n\nBody")]


class FailingDocumentsService:
    def documents(self) -> "FailingDocumentsService":
        return self

    def get(
        self, documentId: str, includeTabsContent: bool
    ) -> "FailingDocumentsService":
        return self

    def execute(self) -> dict[str, object]:
        raise HttpError(
            httplib2.Response({"status": "403"}),
            b'{"error": {"message": "sensitive document data"}}',
        )


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None


class FakeOpener:
    def __init__(self, status: int, events: list[str]) -> None:
        self.status = status
        self.events = events
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        self.events.append("post")
        self.requests.append(request)
        self.timeouts.append(timeout)
        return FakeResponse(self.status)


class FakeMailbox:
    def __init__(self, events: list[str]) -> None:
        self.is_readonly: bool | None = None
        self.seen_message_identifiers: list[bytes] = []
        self.did_logout = False
        self.events = events

    def select(self, folder_name: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.is_readonly = readonly
        return "OK", []

    def store(
        self, message_identifier: bytes, command: str, flags: str
    ) -> tuple[str, list[bytes]]:
        assert command == "+FLAGS"
        assert flags == r"(\Seen)"
        self.events.append("seen")
        self.seen_message_identifiers.append(message_identifier)
        return "OK", []

    def logout(self) -> None:
        self.did_logout = True


def payload() -> dict[str, str | list[dict[str, str]]]:
    return {
        "title": "Synthetic meeting",
        "date": "2026-01-01",
        "invitees": [{"name": "Example attendee", "email": "attendee@example.test"}],
        "summary": "Synthetic summary",
        "decisions": "Synthetic decision",
        "next_steps": "Synthetic next step",
    }


def synchronize_with_response(
    response_status: int, failure_expected: bool, message_identifiers: list[bytes]
) -> tuple[FakeMailbox, FakeOpener, Request, str, object]:
    events: list[str] = []
    mailbox = FakeMailbox(events)
    opener = FakeOpener(response_status, events)
    output = StringIO()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(transcript_sync, "connect_mailbox", return_value=mailbox)
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "search_messages",
                return_value=message_identifiers,
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync, "create_documents_service", return_value=object()
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "fetch_message", return_value=object())
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "first_document_identifier",
                return_value="synthetic-document",
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "fetch_document",
                return_value={"title": "Synthetic meeting"},
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "extract_sections", return_value=payload())
        )
        build_opener_mock = stack.enter_context(
            patch.object(transcript_sync, "build_opener", return_value=opener)
        )
        with patch.dict(
            os.environ,
            {"STAVROBOT_BASE_URL": "https://example.test/stavrobot/"},
            clear=True,
        ):
            with redirect_stdout(output):
                if failure_expected:
                    with unittest.TestCase().assertRaisesRegex(
                        RuntimeError, "non-success status"
                    ):
                        transcript_sync.synchronize("Synthetic folder")
                else:
                    transcript_sync.synchronize("Synthetic folder")
    assert opener.requests
    return (
        mailbox,
        opener,
        opener.requests[0],
        output.getvalue(),
        build_opener_mock.call_args.args[0],
    )


def test_extraction_regressions() -> None:
    document = example_document()
    sections = transcript_sync.extract_sections(document)
    assert list(sections) == [
        "title",
        "date",
        "invitees",
        "summary",
        "decisions",
        "next_steps",
    ]
    assert sections == {
        "title": "Example meeting",
        "date": "2026-01-01",
        "invitees": [{"name": "Example attendee", "email": "attendee@example.test"}],
        "summary": "Concise overview\n- First point",
        "decisions": "Approve the proposal",
        "next_steps": "- Send follow-up",
    }

    utc_date_document = copy.deepcopy(document)
    utc_date_properties = utc_date_document["tabs"][0]["childTabs"][0]["documentTab"][
        "body"
    ]["content"][0]["paragraph"]["elements"][0]["dateElement"]["dateElementProperties"]
    del utc_date_properties["timeZoneId"]
    assert transcript_sync.extract_sections(utc_date_document)["date"] == "2026-01-02"

    malformed_timestamp_document = copy.deepcopy(document)
    malformed_timestamp_properties = malformed_timestamp_document["tabs"][0][
        "childTabs"
    ][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "dateElement"
    ]["dateElementProperties"]
    assert isinstance(malformed_timestamp_properties, dict)
    malformed_timestamp_properties["timestamp"] = "sensitive-timestamp-value"
    with unittest.TestCase().assertRaisesRegex(
        RuntimeError, "Invalid date timestamp"
    ) as error_context:
        transcript_sync.extract_sections(malformed_timestamp_document)
    assert error_context.exception.__cause__ is None
    assert "sensitive-timestamp-value" not in "".join(
        traceback.format_exception(error_context.exception)
    )

    malformed_time_zone_document = copy.deepcopy(document)
    malformed_time_zone_properties = malformed_time_zone_document["tabs"][0][
        "childTabs"
    ][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0][
        "dateElement"
    ]["dateElementProperties"]
    assert isinstance(malformed_time_zone_properties, dict)
    malformed_time_zone_properties["timeZoneId"] = "sensitive-time-zone-value"
    with unittest.TestCase().assertRaisesRegex(
        RuntimeError, "Invalid date time zone"
    ) as error_context:
        transcript_sync.extract_sections(malformed_time_zone_document)
    assert error_context.exception.__cause__ is None
    assert "sensitive-time-zone-value" not in "".join(
        traceback.format_exception(error_context.exception)
    )

    no_summary_document: dict[str, object] = {
        "title": "Example meeting",
        "tabs": [tab([paragraph("Raw transcript\n", "TITLE")])],
    }
    with unittest.TestCase().assertRaisesRegex(
        RuntimeError, "exactly one tab with a Summary heading"
    ):
        transcript_sync.extract_sections(no_summary_document)

    missing_date_document = copy.deepcopy(document)
    missing_date_content = missing_date_document["tabs"][0]["childTabs"][0][
        "documentTab"
    ]["body"]["content"]
    assert isinstance(missing_date_content, list)
    missing_date_content[0]["paragraph"]["elements"] = []
    with unittest.TestCase().assertRaisesRegex(RuntimeError, "date element"):
        transcript_sync.extract_sections(missing_date_document)

    duplicate_date_document = copy.deepcopy(document)
    duplicate_date_content = duplicate_date_document["tabs"][0]["childTabs"][0][
        "documentTab"
    ]["body"]["content"]
    assert isinstance(duplicate_date_content, list)
    duplicate_date_content[0]["paragraph"]["elements"].append(
        date_element("2026-01-02T00:30:00Z", "America/Los_Angeles")
    )
    with unittest.TestCase().assertRaisesRegex(
        RuntimeError, "exactly one date element"
    ):
        transcript_sync.extract_sections(duplicate_date_document)

    missing_decisions_document = copy.deepcopy(document)
    missing_decisions_content = missing_decisions_document["tabs"][0]["childTabs"][0][
        "documentTab"
    ]["body"]["content"]
    assert isinstance(missing_decisions_content, list)
    missing_decisions_content[:] = [
        structural_element
        for structural_element in missing_decisions_content
        if structural_element["paragraph"]["elements"][0]
        .get("textRun", {})
        .get("content")
        != "Decisions\n"
    ]
    assert (
        transcript_sync.extract_sections(missing_decisions_document)["decisions"] == ""
    )

    duplicate_decisions_document = copy.deepcopy(document)
    duplicate_decisions_content = duplicate_decisions_document["tabs"][0]["childTabs"][
        0
    ]["documentTab"]["body"]["content"]
    assert isinstance(duplicate_decisions_content, list)
    duplicate_decisions_content.append(paragraph("Decisions\n", "HEADING_3"))
    with unittest.TestCase().assertRaisesRegex(RuntimeError, "decisions heading"):
        transcript_sync.extract_sections(duplicate_decisions_document)

    empty_decisions_document = copy.deepcopy(document)
    empty_decisions_content = empty_decisions_document["tabs"][0]["childTabs"][0][
        "documentTab"
    ]["body"]["content"]
    assert isinstance(empty_decisions_content, list)
    empty_decisions_content[:] = [
        structural_element
        for structural_element in empty_decisions_content
        if structural_element["paragraph"]["elements"][0]
        .get("textRun", {})
        .get("content")
        != "Approve the proposal\n"
    ]
    assert transcript_sync.extract_sections(empty_decisions_document)["decisions"] == ""


def test_safe_document_and_message_fetching() -> None:
    mailbox = FetchingMailbox()
    transcript_sync.fetch_message(cast(imaplib.IMAP4_SSL, mailbox), b"1")
    assert mailbox.fetch_query == "(BODY.PEEK[])"

    with unittest.TestCase().assertRaisesRegex(
        RuntimeError, "Google Docs request failed with HTTP status 403"
    ) as error_context:
        transcript_sync.fetch_document(FailingDocumentsService(), "synthetic-document")
    assert error_context.exception.__cause__ is None
    assert "sensitive document data" not in str(error_context.exception)


def test_synchronize_delivery_order() -> None:
    failed_mailbox, failed_opener, _, failed_output, _ = synchronize_with_response(
        500, True, [b"1", b"2"]
    )
    assert failed_mailbox.seen_message_identifiers == []
    assert failed_opener.events == ["post"]
    assert failed_output == (
        "Processing 'Synthetic meeting' (synthetic-document)\n"
        "Processing 2026-01-01: Synthetic meeting\n"
    )

    redirected_mailbox, redirected_opener, _, redirected_output, redirect_handler = (
        synchronize_with_response(302, True, [b"1"])
    )
    assert redirected_mailbox.seen_message_identifiers == []
    assert redirected_opener.events == ["post"]
    assert redirected_output == (
        "Processing 'Synthetic meeting' (synthetic-document)\n"
        "Processing 2026-01-01: Synthetic meeting\n"
    )
    assert isinstance(redirect_handler, transcript_sync.NoRedirect)
    # The annotation is not enforced at runtime, and FakeOpener never exercises
    # NoRedirect, so this is the only check that a POST is not replayed at a
    # redirect target.
    assert (
        redirect_handler.redirect_request(  # type: ignore[func-returns-value]
            Request("https://example.test"),
            object(),
            302,
            "Found",
            object(),
            "https://other.example.test",
        )
        is None
    )

    mailbox, opener, request, output, _ = synchronize_with_response(
        202, False, [b"12", b"3"]
    )
    assert mailbox.is_readonly is False
    assert mailbox.seen_message_identifiers == [b"3", b"12"]
    assert opener.events == ["post", "seen", "post", "seen"]
    assert mailbox.did_logout is True
    assert output == (
        "Processing 'Synthetic meeting' (synthetic-document)\n"
        "Processing 2026-01-01: Synthetic meeting\n"
        "Processing 'Synthetic meeting' (synthetic-document)\n"
        "Processing 2026-01-01: Synthetic meeting\n"
        "Done.\n"
    )
    assert opener.timeouts == [120, 120]
    assert request.full_url == "https://example.test/stavrobot/chat"
    assert isinstance(request.data, bytes)
    request_payload = json.loads(request.data)
    assert request_payload == transcript_sync.chat_request(payload())
    assert request.get_header("Authorization") is None


def test_synchronize_leaves_invalid_documents_unread() -> None:
    events: list[str] = []
    mailbox = FakeMailbox(events)
    invalid_title_document = example_document()
    invalid_title_document["title"] = ""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(transcript_sync, "connect_mailbox", return_value=mailbox)
        )
        stack.enter_context(
            patch.object(transcript_sync, "search_messages", return_value=[b"1"])
        )
        stack.enter_context(
            patch.object(
                transcript_sync, "create_documents_service", return_value=object()
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "fetch_message", return_value=object())
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "first_document_identifier",
                return_value="synthetic-document",
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "fetch_document",
                return_value=invalid_title_document,
            )
        )
        print_mock = stack.enter_context(patch("builtins.print"))
        with unittest.TestCase().assertRaisesRegex(RuntimeError, "document title"):
            transcript_sync.synchronize("Synthetic folder")

    assert mailbox.seen_message_identifiers == []
    assert events == []
    print_mock.assert_called_once_with("Processing '' (synthetic-document)", flush=True)


def test_preview_is_read_only_and_prints_json() -> None:
    events: list[str] = []
    mailbox = FakeMailbox(events)
    output = StringIO()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(transcript_sync, "connect_mailbox", return_value=mailbox)
        )
        search_messages_mock = stack.enter_context(
            patch.object(
                transcript_sync,
                "search_messages",
                return_value=[b"12", b"3"],
            )
        )
        fetch_message_mock = stack.enter_context(
            patch.object(transcript_sync, "fetch_message", return_value=object())
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "first_document_identifier",
                return_value="synthetic-document",
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync, "create_documents_service", return_value=object()
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "fetch_document", return_value={})
        )
        stack.enter_context(
            patch.object(transcript_sync, "extract_sections", return_value=payload())
        )
        post_payload_mock = stack.enter_context(
            patch.object(transcript_sync, "post_payload")
        )
        with redirect_stdout(output):
            transcript_sync.preview("Synthetic folder")

    assert mailbox.is_readonly is True
    assert mailbox.did_logout is True
    assert events == []
    assert search_messages_mock.call_args.args == (mailbox, "ALL")
    assert fetch_message_mock.call_args.args == (mailbox, b"12")
    post_payload_mock.assert_not_called()
    assert json.loads(output.getvalue()) == payload()


def test_chat_request_rendering() -> None:
    assert transcript_sync.chat_request(payload()) == {
        "message": "Here is the data that was parsed from a recent meeting:\n\n"
        "Title: Synthetic meeting\n"
        "Date: 2026-01-01\n"
        "Invitees:\n"
        "- Example attendee <attendee@example.test>\n\n"
        "Summary:\nSynthetic summary\n\n"
        "Decisions:\nSynthetic decision\n\n"
        "Next steps:\nSynthetic next step",
        "source": "panopticon",
        "sender": "panopticon",
        "async": True,
    }


def test_post_preserves_optional_bearer_token() -> None:
    events: list[str] = []
    opener = FakeOpener(202, events)
    with patch.object(transcript_sync, "build_opener", return_value=opener):
        with patch.dict(
            os.environ,
            {
                "STAVROBOT_BASE_URL": "https://example.test/stavrobot",
                "TRANSCRIPT_ENDPOINT_TOKEN": "synthetic-token",
            },
            clear=True,
        ):
            transcript_sync.post_payload(transcript_sync.chat_request(payload()))

    assert opener.requests[0].get_header("Authorization") == "Bearer synthetic-token"


def test_post_uses_decoded_basic_credentials_without_userinfo_in_url() -> None:
    events: list[str] = []
    opener = FakeOpener(202, events)
    with patch.object(transcript_sync, "build_opener", return_value=opener):
        with patch.dict(
            os.environ,
            {
                "STAVROBOT_BASE_URL": (
                    "https://example%20user:password%3Awith%40symbols@"
                    "example.test/stavrobot"
                )
            },
            clear=True,
        ):
            transcript_sync.post_payload(transcript_sync.chat_request(payload()))

    request = opener.requests[0]
    assert request.full_url == "https://example.test/stavrobot/chat"
    assert (
        request.get_header("Authorization")
        == "Basic ZXhhbXBsZSB1c2VyOnBhc3N3b3JkOndpdGhAc3ltYm9scw=="
    )


def test_post_rejects_combined_basic_and_bearer_authentication_before_network() -> None:
    build_opener_mock = Mock()
    with patch.object(transcript_sync, "build_opener", build_opener_mock):
        with patch.dict(
            os.environ,
            {
                "STAVROBOT_BASE_URL": "https://user:password@example.test/stavrobot",
                "TRANSCRIPT_ENDPOINT_TOKEN": "synthetic-token",
            },
            clear=True,
        ):
            with unittest.TestCase().assertRaisesRegex(
                ValueError, "either Basic authentication or a bearer token"
            ):
                transcript_sync.post_payload(transcript_sync.chat_request(payload()))

    build_opener_mock.assert_not_called()


def test_dry_run_is_read_only_and_needs_no_endpoint_configuration() -> None:
    events: list[str] = []
    mailbox = FakeMailbox(events)
    output = StringIO()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(transcript_sync, "connect_mailbox", return_value=mailbox)
        )
        stack.enter_context(
            patch.object(transcript_sync, "search_messages", return_value=[b"12", b"3"])
        )
        stack.enter_context(
            patch.object(
                transcript_sync, "create_documents_service", return_value=object()
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "fetch_message", return_value=object())
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "first_document_identifier",
                return_value="synthetic-document",
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "fetch_document",
                return_value={"title": "Synthetic meeting"},
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "extract_sections", return_value=payload())
        )
        post_payload_mock = stack.enter_context(
            patch.object(transcript_sync, "post_payload")
        )
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stdout(output):
                transcript_sync.synchronize("Synthetic folder", dry_run=True)

    assert mailbox.is_readonly is True
    assert mailbox.seen_message_identifiers == []
    assert mailbox.did_logout is True
    assert events == []
    post_payload_mock.assert_not_called()
    message = transcript_sync.chat_request(payload())["message"]
    assert output.getvalue() == (
        "Processing 'Synthetic meeting' (synthetic-document)\n"
        f"{message}\n"
        "Processing 'Synthetic meeting' (synthetic-document)\n"
        f"---\n{message}\nDone.\n"
    )


def test_dry_run_failure_leaves_messages_unread() -> None:
    events: list[str] = []
    mailbox = FakeMailbox(events)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(transcript_sync, "connect_mailbox", return_value=mailbox)
        )
        stack.enter_context(
            patch.object(transcript_sync, "search_messages", return_value=[b"1"])
        )
        stack.enter_context(
            patch.object(
                transcript_sync, "create_documents_service", return_value=object()
            )
        )
        stack.enter_context(
            patch.object(transcript_sync, "fetch_message", return_value=object())
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "first_document_identifier",
                return_value="synthetic-document",
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "fetch_document",
                return_value={"title": "Example meeting"},
            )
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "extract_sections",
                side_effect=RuntimeError("Missing or invalid date element"),
            )
        )
        print_mock = stack.enter_context(patch("builtins.print"))
        with unittest.TestCase().assertRaisesRegex(RuntimeError, "date element"):
            transcript_sync.synchronize("Synthetic folder", dry_run=True)

    assert mailbox.is_readonly is True
    assert mailbox.seen_message_identifiers == []
    assert events == []
    print_mock.assert_called_once_with(
        "Processing 'Example meeting' (synthetic-document)", flush=True
    )


def test_empty_mailbox_finishes_without_google_credentials() -> None:
    events: list[str] = []
    mailbox = FakeMailbox(events)
    output = StringIO()
    create_documents_service_mock = Mock()
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(transcript_sync, "connect_mailbox", return_value=mailbox)
        )
        stack.enter_context(
            patch.object(transcript_sync, "search_messages", return_value=[])
        )
        stack.enter_context(
            patch.object(
                transcript_sync,
                "create_documents_service",
                create_documents_service_mock,
            )
        )
        with redirect_stdout(output):
            transcript_sync.synchronize("Synthetic folder")

    create_documents_service_mock.assert_not_called()
    assert output.getvalue() == "Done.\n"
    assert mailbox.seen_message_identifiers == []


def test_authenticate_requires_authorize_for_missing_token_cache() -> None:
    with TemporaryDirectory() as temporary_directory:
        token_cache_path = Path(temporary_directory) / "google_oauth_token.json"
        with patch.object(
            transcript_sync, "InstalledAppFlow"
        ) as installed_app_flow_mock:
            with patch.dict(
                os.environ,
                {"GOOGLE_OAUTH_TOKEN_CACHE_PATH": str(token_cache_path)},
                clear=True,
            ):
                with unittest.TestCase().assertRaises(RuntimeError) as error_context:
                    transcript_sync.authenticate()

    assert str(token_cache_path) in str(error_context.exception)
    assert "transcript_sync.py authorize" in str(error_context.exception)
    installed_app_flow_mock.assert_not_called()


def test_command_routing() -> None:
    preview_mock = Mock()
    synchronize_mock = Mock()
    authorize_mock = Mock()
    with patch.object(transcript_sync, "preview", preview_mock):
        with patch.object(transcript_sync, "synchronize", synchronize_mock):
            with patch.object(transcript_sync, "authorize", authorize_mock):
                with patch.object(
                    sys, "argv", ["transcript_sync.py", "preview", "Folder"]
                ):
                    transcript_sync.main()
                with patch.object(
                    sys, "argv", ["transcript_sync.py", "sync", "Folder"]
                ):
                    transcript_sync.main()
                with patch.object(
                    sys, "argv", ["transcript_sync.py", "sync", "--dry-run", "Folder"]
                ):
                    transcript_sync.main()
                with patch.object(sys, "argv", ["transcript_sync.py", "authorize"]):
                    transcript_sync.main()
    preview_mock.assert_called_once_with("Folder")
    assert synchronize_mock.call_args_list == [
        unittest.mock.call("Folder", dry_run=False),
        unittest.mock.call("Folder", dry_run=True),
    ]
    authorize_mock.assert_called_once_with()


def main() -> None:
    test_extraction_regressions()
    test_safe_document_and_message_fetching()
    test_synchronize_delivery_order()
    test_synchronize_leaves_invalid_documents_unread()
    test_preview_is_read_only_and_prints_json()
    test_chat_request_rendering()
    test_post_preserves_optional_bearer_token()
    test_post_uses_decoded_basic_credentials_without_userinfo_in_url()
    test_post_rejects_combined_basic_and_bearer_authentication_before_network()
    test_dry_run_is_read_only_and_needs_no_endpoint_configuration()
    test_dry_run_failure_leaves_messages_unread()
    test_empty_mailbox_finishes_without_google_credentials()
    test_authenticate_requires_authorize_for_missing_token_cache()
    test_command_routing()


if __name__ == "__main__":
    main()
