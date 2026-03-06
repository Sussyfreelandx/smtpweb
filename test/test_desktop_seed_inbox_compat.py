from app.core_logic.desktop_seed_inbox_compat import parse_seed_emails, _extract_to_emails


def test_parse_seed_emails():
    seeds = parse_seed_emails("Seed1@Example.com\ninvalid\nseed2@example.org\n\nseed1@example.com")
    assert seeds == ["seed1@example.com", "seed2@example.org"]


def test_extract_to_emails():
    raw = b"To: Seed One <seed1@example.com>, seed2@example.org\r\nSubject: hi\r\n\r\n"
    emails = _extract_to_emails(raw)
    assert "seed1@example.com" in emails
    assert "seed2@example.org" in emails
