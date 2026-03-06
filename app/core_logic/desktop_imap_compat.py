"""
Phase 7 compatibility helpers for desktop-style IMAP reply checking.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parseaddr
import imaplib
import re
import ssl


def normalize_email_address(value: str | None) -> str:
    addr = parseaddr(value or '')[1]
    return addr.strip().lower()


def extract_sender_emails_from_messages(raw_messages: list[bytes]) -> set[str]:
    senders = set()
    for raw in raw_messages:
        try:
            msg = message_from_bytes(raw)
            sender = normalize_email_address(msg.get('From', ''))
            if sender and '@' in sender:
                senders.add(sender)
        except Exception:
            continue
    return senders


def extract_reply_signals_from_messages(raw_messages: list[bytes]) -> dict:
    senders = set()
    reply_message_ids = set()
    for raw in raw_messages:
        try:
            msg = message_from_bytes(raw)
            sender = normalize_email_address(msg.get('From', ''))
            if sender and '@' in sender:
                senders.add(sender)

            in_reply_to = msg.get('In-Reply-To', '') or ''
            references = msg.get('References', '') or ''
            combined = f"{in_reply_to} {references}".strip()
            if combined:
                ids = re.findall(r'<[^>]+>', combined)
                if ids:
                    reply_message_ids.update(ids)
        except Exception:
            continue
    return {
        'sender_emails': senders,
        'reply_message_ids': reply_message_ids,
    }


def fetch_recent_imap_sender_emails(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = 'INBOX',
    limit: int = 200,
    allow_insecure_ssl: bool = False,
) -> set[str]:
    if not host or not username or not password:
        return set()

    context = ssl.create_default_context()
    if allow_insecure_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=context)
        conn.login(username, password)
        conn.select(mailbox, readonly=True)
        since_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%d-%b-%Y')
        status, data = conn.search(None, 'SINCE', since_date)
        if status != 'OK':
            status, data = conn.search(None, 'ALL')
        if status != 'OK':
            return set()
        # For the returned search result set, we take the tail as an approximation of newest messages.
        # Sequence IDs can shift after deletions; this tradeoff is acceptable for periodic polling.
        # If fewer than `limit` items are returned, slicing intentionally yields all available IDs.
        all_ids = data[0].split() if data and data[0] else []
        ids = all_ids[-limit:]
        raw_messages = []
        for mid in ids:
            f_status, parts = conn.fetch(mid, '(BODY.PEEK[HEADER])')
            if f_status != 'OK' or not parts:
                continue
            for part in parts:
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                    raw_messages.append(part[1])
        return extract_sender_emails_from_messages(raw_messages)
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass


def fetch_recent_imap_reply_signals(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = 'INBOX',
    limit: int = 200,
    allow_insecure_ssl: bool = False,
) -> dict:
    if not host or not username or not password:
        return {'sender_emails': set(), 'reply_message_ids': set()}

    context = ssl.create_default_context()
    if allow_insecure_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=context)
        conn.login(username, password)
        conn.select(mailbox, readonly=True)
        since_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%d-%b-%Y')
        status, data = conn.search(None, 'SINCE', since_date)
        if status != 'OK':
            status, data = conn.search(None, 'ALL')
        if status != 'OK':
            return {'sender_emails': set(), 'reply_message_ids': set()}
        ids = (data[0].split() if data and data[0] else [])[-limit:]
        raw_messages = []
        for mid in ids:
            f_status, parts = conn.fetch(mid, '(BODY.PEEK[HEADER])')
            if f_status != 'OK' or not parts:
                continue
            for part in parts:
                if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                    raw_messages.append(part[1])
        return extract_reply_signals_from_messages(raw_messages)
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass
