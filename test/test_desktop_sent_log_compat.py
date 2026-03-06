from datetime import datetime

from app.core_logic.desktop_sent_log_compat import append_sent_log, format_recipient_log_entry


class DummyRecipient:
    def __init__(self, email, status, status_message="", sent_at=None, last_attempt_at=None, created_at=None):
        self.email = email
        self.status = status
        self.status_message = status_message
        self.sent_at = sent_at
        self.last_attempt_at = last_attempt_at
        self.created_at = created_at or datetime.utcnow()


def test_append_sent_log_enforces_cap():
    entries = []
    for i in range(10001):
        entries = append_sent_log(entries, {"idx": i})
    assert len(entries) == 5000
    assert entries[0]["idx"] == 5001
    assert entries[-1]["idx"] == 10000


def test_format_recipient_log_entry_success():
    recipient = DummyRecipient(
        email="user@example.com",
        status="Sent",
        status_message="OK",
        sent_at=datetime(2026, 1, 1, 10, 30, 0),
    )
    entry = format_recipient_log_entry(recipient)
    assert entry["timestamp"] == "[10:30:00]"
    assert entry["severity"] == "SUCCESS"
    assert entry["email"] == "user@example.com"
    assert entry["status"] == "Sent"
    assert entry["message"] == "OK"


def test_format_recipient_log_entry_failed_fallback_time():
    recipient = DummyRecipient(
        email="fail@example.com",
        status="Failed",
        status_message="smtp auth failed",
        sent_at=None,
        last_attempt_at=datetime(2026, 1, 1, 11, 0, 0),
    )
    entry = format_recipient_log_entry(recipient)
    assert entry["timestamp"] == "[11:00:00]"
    assert entry["severity"] == "ERROR"
    assert entry["message"] == "smtp auth failed"

