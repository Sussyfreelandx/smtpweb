"""
Phase 5 compatibility helpers for desktop-style sent email log handling.
"""

from __future__ import annotations

from datetime import datetime


def append_sent_log(entries: list[dict], entry: dict, max_entries: int = 10000, trim_to: int = 5000) -> list[dict]:
    """
    Append a log entry and enforce desktop-style cap behavior.
    """
    entries.append(entry)
    if len(entries) > max_entries:
        entries = entries[-trim_to:]
    return entries


def format_recipient_log_entry(recipient) -> dict:
    """
    Convert a recipient row into a UI-friendly log entry.
    """
    ts = recipient.sent_at or recipient.last_attempt_at or recipient.created_at or datetime.utcnow()
    status = recipient.status or "Unknown"
    if status.lower() == "sent":
        severity = "SUCCESS"
    elif status.lower() == "failed":
        severity = "ERROR"
    elif status.lower() in {"sending", "queued"}:
        severity = "INFO"
    else:
        severity = "WARNING"

    message = recipient.status_message or status
    return {
        "timestamp": ts.strftime("[%H:%M:%S]"),
        "severity": severity,
        "email": recipient.email,
        "status": status,
        "message": message[:250],
    }

