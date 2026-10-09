"""An API failure must name the files it was carrying, and say what to do.

Chasing a 500 on 2026-08-06 there was no record of what the failing request
contained — only an HTTP status. These lock in the manifest and the messages.
"""

import base64
import logging

import pytest

from app.models import UploadedDoc
from app import extraction


def _doc(filename: str, size: int = 300, mime: str = "") -> UploadedDoc:
    return UploadedDoc(filename=filename, mime=mime,
                       b64=base64.b64encode(b"x" * size).decode())


class _FakeError(Exception):
    """Stands in for anthropic.APIError, which carries these attributes."""
    def __init__(self, message, status_code=None, request_id=None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


def test_manifest_names_every_file(caplog):
    docs = [_doc("PFS.pdf"), _doc("terms.docx")]
    content = [
        {"type": "document", "source": {"data": docs[0].b64}},
        {"type": "text", "text": "Loan Amount | $2,025,000"},
    ]
    with caplog.at_level(logging.INFO, logger="app.extraction"):
        extraction.log_request_manifest("extraction", docs, content)
    logged = caplog.text
    assert "PFS.pdf" in logged and "terms.docx" in logged
    assert "2 document(s)" in logged


def test_manifest_sizes_a_converted_file_by_its_text_not_its_upload(caplog):
    # An 8 MB .docx becomes a few KB of text — the manifest must not imply the
    # request was 8 MB, or it points the next investigation at the wrong file.
    big = _doc("Closing Packet.docx", size=8_000_000)
    content = [{"type": "text", "text": "short extracted text"}]
    with caplog.at_level(logging.INFO, logger="app.extraction"):
        extraction.log_request_manifest("extraction", [big], content)
    assert "of text" in caplog.text
    assert "~0.0 MB sent" in caplog.text


def test_oversized_request_is_warned_about(caplog):
    docs = [_doc("huge.pdf", size=40)]
    content = [{"type": "text", "text": "y" * 20_000_000}]
    with caplog.at_level(logging.INFO, logger="app.extraction"):
        extraction.log_request_manifest("extraction", docs, content)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "fewer documents" in caplog.text


def test_oversized_request_is_refused_before_the_api_call():
    # 2026-08-11: a ~26 MB raw upload encoded to ~35 MB and the API 413'd with
    # "Request exceeds the maximum size" — naming nothing. The pre-flight check
    # must refuse first and name the largest files.
    docs = [_doc("Supporting Documents.pdf"), _doc("PFS.pdf")]
    content = [
        {"type": "document", "source": {"data": "x" * 31_000_000}},
        {"type": "document", "source": {"data": "y" * 2_000_000}},
    ]
    with pytest.raises(RuntimeError) as exc:
        extraction.check_request_size("extraction", docs, content)
    msg = str(exc.value)
    assert "Supporting Documents.pdf" in msg      # the file to act on
    assert "too large" in msg                     # keeps the UI wording family
    assert "Step 2" in msg                        # what to do about lost fields


def test_request_within_budget_is_not_refused():
    docs = [_doc("PFS.pdf")]
    content = [{"type": "document", "source": {"data": "x" * 5_000_000}}]
    extraction.check_request_size("extraction", docs, content)  # must not raise


@pytest.mark.parametrize("status", [500, 502, 503, 529])
def test_server_errors_are_explained_as_anthropic_side(status):
    msg = extraction.describe_api_error(
        _FakeError("Internal server error", status_code=status,
                   request_id="req_abc123"), "extraction")
    assert "not with your documents" in msg
    assert "req_abc123" in msg
    assert "fewer documents" in msg          # the one thing she can act on


def test_error_without_a_request_id_still_reads_cleanly():
    msg = extraction.describe_api_error(
        _FakeError("Internal server error", status_code=500), "extraction")
    assert "unknown" in msg


def test_non_server_errors_keep_the_original_detail():
    msg = extraction.describe_api_error(
        _FakeError("media_type must be application/pdf", status_code=400,
                   request_id="req_z"), "extraction")
    assert "media_type" in msg
    assert "req_z" in msg


class _FlakyClient:
    """Fails with the given error a set number of times, then succeeds."""
    def __init__(self, error, failures):
        self.error, self.failures, self.attempts = error, failures, 0
        client = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                client.attempts += 1
                if client.attempts <= client.failures:
                    raise client.error
                return "the-message"
        self.messages = _Messages()


def test_create_with_retry_retries_a_500_the_sdk_gave_up_on(monkeypatch):
    # 2026-08-10 incident: the API answered a valid 11-document upload with a
    # 500 AND x-should-retry: false, so the SDK made ONE attempt and quit —
    # while a manual retry succeeded. The app-level wrapper must retry any
    # 5xx regardless, with the configured spacing.
    sleeps = []
    monkeypatch.setattr(extraction.time, "sleep", sleeps.append)
    client = _FlakyClient(_FakeError("api_error", status_code=500,
                                     request_id="req_1"), failures=2)
    assert extraction.create_with_retry(client, "extraction") == "the-message"
    assert client.attempts == 3
    assert sleeps == list(extraction._RETRY_DELAYS_S[:2])


def test_create_with_retry_raises_after_the_delays_are_exhausted(monkeypatch):
    monkeypatch.setattr(extraction.time, "sleep", lambda s: None)
    client = _FlakyClient(_FakeError("api_error", status_code=500),
                          failures=99)
    with pytest.raises(_FakeError):
        extraction.create_with_retry(client, "extraction")
    assert client.attempts == len(extraction._RETRY_DELAYS_S) + 1


def test_create_with_retry_never_retries_client_errors(monkeypatch):
    # Auth/validation/too-large failures are not transient — retrying them
    # would just make the user wait ~a minute for the same error.
    monkeypatch.setattr(extraction.time, "sleep",
                        lambda s: pytest.fail("must not sleep on a 4xx"))
    client = _FlakyClient(_FakeError("bad request", status_code=400),
                          failures=99)
    with pytest.raises(_FakeError):
        extraction.create_with_retry(client, "extraction")
    assert client.attempts == 1


def test_retries_are_configured_above_the_sdk_default():
    # The SDK's default of 2 was not enough: uploads that failed succeeded on a
    # manual retry moments later.
    assert extraction._MAX_RETRIES > 2

    captured = {}

    class _FakeAnthropic:
        def Anthropic(self, **kwargs):        # noqa: N802 - mimics the SDK
            captured.update(kwargs)
            return object()

    extraction.build_client(_FakeAnthropic(), "sk-ant-oat01-test")
    assert captured["max_retries"] == extraction._MAX_RETRIES
    assert captured["auth_token"] == "sk-ant-oat01-test"


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    """Stands in for the SDK's Message: text blocks plus a stop_reason."""

    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


def test_parse_json_reply_strips_markdown_fences():
    msg = _FakeMessage('```json\n{"borrower_name": "T"}\n```')
    assert extraction.parse_json_reply(msg, "extraction") == {"borrower_name": "T"}


def test_a_truncated_reply_is_explained_not_dumped_as_a_parse_error():
    # 2026-08-20 incident: an 11-document upload's reply outgrew the response
    # token budget, and the UI showed the raw JSONDecodeError ("Unterminated
    # string starting at: line 155 column 28") with nothing to act on.
    msg = _FakeMessage('{"borrower_name": "Jalen Two-Riv',
                       stop_reason="max_tokens")
    with pytest.raises(RuntimeError) as exc:
        extraction.parse_json_reply(msg, "extraction")
    assert "cut off" in str(exc.value)
    assert "fewer documents" in str(exc.value)     # the one thing she can act on
    assert "Unterminated" not in str(exc.value)


def test_a_malformed_reply_still_reports_the_parse_error():
    # Only truncation gets the special wording — genuine garbage keeps the
    # underlying JSON error so it stays diagnosable.
    msg = _FakeMessage("I cannot extract these documents.")
    with pytest.raises(RuntimeError) as exc:
        extraction.parse_json_reply(msg, "extraction")
    assert "Could not parse extraction response" in str(exc.value)


def test_parse_json_reply_salvages_json_wrapped_in_prose():
    """2026-08-25: the salary cross-check reply opened with a sentence before
    the JSON and the bare json.loads failed with "Expecting value: line 1
    column 1" - the UI then showed "cross-check could not be run" even though
    Spotrac had been fetched fine. The outermost {...} span must parse."""
    msg = _FakeMessage(
        'Here is the verification result you asked for:\n'
        '{"spotrac_salary": 9000000, "season": "2026", "note": "Payroll salary '
        'as Spotrac lists it."}\n'
        'Let me know if anything else is needed.')
    data = extraction.parse_json_reply(msg, "salary cross-check")
    assert data["spotrac_salary"] == 9000000
    assert data["season"] == "2026"


def test_parse_json_reply_still_reports_a_truly_broken_reply():
    import pytest as _pytest
    msg = _FakeMessage("I could not find any contract data on that page.")
    with _pytest.raises(RuntimeError):
        extraction.parse_json_reply(msg, "salary cross-check")
