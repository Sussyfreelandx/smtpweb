from app.core_logic.desktop_imap_compat import (
    extract_sender_emails_from_messages,
    extract_reply_signals_from_messages,
    normalize_email_address,
)


def test_normalize_email_address():
    assert normalize_email_address("John Doe <John.Doe@Example.COM>") == "john.doe@example.com"
    assert normalize_email_address("plain@example.com") == "plain@example.com"
    assert normalize_email_address(None) == ""


def test_extract_sender_emails_from_messages():
    raw1 = b"From: Alice <alice@example.com>\r\nSubject: hi\r\n\r\n"
    raw2 = b"From: bob@example.org\r\nSubject: hello\r\n\r\n"
    raw3 = b"Not-A-Valid-Header"
    senders = extract_sender_emails_from_messages([raw1, raw2, raw3])
    assert "alice@example.com" in senders
    assert "bob@example.org" in senders
    assert len(senders) == 2


def test_extract_reply_signals_from_messages():
    raw = (
        b"From: Reply User <reply@example.com>\r\n"
        b"In-Reply-To: <msg-123@example.com>\r\n"
        b"References: <root@example.com> <msg-123@example.com>\r\n"
        b"Subject: Re: hi\r\n\r\n"
    )
    signals = extract_reply_signals_from_messages([raw])
    assert "reply@example.com" in signals["sender_emails"]
    assert "<msg-123@example.com>" in signals["reply_message_ids"]
    assert "<root@example.com>" in signals["reply_message_ids"]
