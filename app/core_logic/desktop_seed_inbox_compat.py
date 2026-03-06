"""
Phase 8 compatibility helpers for desktop-style seed inbox placement checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import getaddresses
import imaplib
import ssl


DEFAULT_SPAM_FOLDERS = ['Junk', 'Spam', 'Bulk', 'Junk E-mail']


def parse_seed_emails(raw_text: str | None) -> list[str]:
    seeds = []
    for line in (raw_text or '').splitlines():
        value = line.strip().lower()
        if value and '@' in value:
            seeds.append(value)
    return sorted(set(seeds))


def _extract_to_emails(raw_header_bytes: bytes) -> set[str]:
    try:
        msg = message_from_bytes(raw_header_bytes)
        recipients = {addr.strip().lower() for _, addr in getaddresses(msg.get_all('To', [])) if addr}
        return recipients
    except Exception:
        return set()


def check_seed_inbox_placement_imap(
    host: str,
    port: int,
    username: str,
    password: str,
    seed_emails: list[str],
    spam_folders: list[str] | None = None,
    allow_insecure_ssl: bool = False,
    fetch_limit: int = 500,
) -> dict:
    """
    Check seed placement by scanning inbox and spam-like folders for To-header seed matches.

    If a seed is found in both inbox and spam folders, spam precedence is applied.
    """
    if not host or not username or not password:
        return {"ok": False, "error": "IMAP credentials are required"}
    if not seed_emails:
        return {"ok": False, "error": "No seed emails provided"}

    spam_folders = spam_folders or DEFAULT_SPAM_FOLDERS
    seed_set = {s.strip().lower() for s in seed_emails if s}
    spam_seeds_found = set()
    inbox_seeds_found = set()

    context = ssl.create_default_context()
    if allow_insecure_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    conn = None
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=context)
        conn.login(username, password)

        folders_to_check = ['INBOX'] + spam_folders
        for folder in folders_to_check:
            status, _ = conn.select(folder, readonly=True)
            if status != 'OK':
                continue
            since_date = (datetime.now(tz=timezone.utc) - timedelta(days=7)).strftime('%d-%b-%Y')
            s_status, data = conn.search(None, 'SINCE', since_date)
            if s_status != 'OK':
                s_status, data = conn.search(None, 'ALL')
            if s_status != 'OK':
                continue
            # Only scan recent message IDs to bound processing cost on large mailboxes.
            ids = (data[0].split() if data and data[0] else [])[-fetch_limit:]
            for mid in ids:
                f_status, parts = conn.fetch(mid, '(BODY.PEEK[HEADER.FIELDS (TO)])')
                if f_status != 'OK' or not parts:
                    continue
                for part in parts:
                    if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                        to_emails = _extract_to_emails(part[1])
                        matched = seed_set.intersection(to_emails)
                        if not matched:
                            continue
                        if folder == 'INBOX':
                            inbox_seeds_found.update(matched)
                        else:
                            spam_seeds_found.update(matched)

        # If a seed appears in both, treat as spam placement (spam precedence).
        inbox_seeds_found = inbox_seeds_found.difference(spam_seeds_found)
        delivered = inbox_seeds_found.union(spam_seeds_found)
        total = len(seed_set)
        spam_rate = round((len(spam_seeds_found) / total) * 100, 2) if total else 0.0
        inbox_rate = round((len(inbox_seeds_found) / total) * 100, 2) if total else 0.0

        return {
            "ok": True,
            "total_seeds": total,
            "delivered_count": len(delivered),
            "inbox_count": len(inbox_seeds_found),
            "spam_count": len(spam_seeds_found),
            "spam_rate": spam_rate,
            "inbox_rate": inbox_rate,
            "inbox_seeds_found": sorted(inbox_seeds_found),
            "spam_seeds_found": sorted(spam_seeds_found),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass
