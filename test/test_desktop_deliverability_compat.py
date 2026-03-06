from app.core_logic.desktop_deliverability_compat import (
    extract_domains_from_text,
    classify_auth_status,
    build_domain_check_row,
)


def test_extract_domains_from_text():
    raw = "example.com\nuser@test.org\n invalid domain \n@bad.com\n\n"
    domains = extract_domains_from_text(raw)
    assert domains == ["example.com", "test.org"]


def test_classify_auth_status():
    assert classify_auth_status("✅ Found", "✅ Found", "✅ Found") == ("✅ All Pass", "pass")
    assert classify_auth_status("❌ Missing", "❌ Missing", "❌ Missing") == ("❌ Critical Issues", "critical")
    assert classify_auth_status("✅ Found", "❌ Missing", "✅ Found") == ("⚠️ 2/3 Pass", "partial")


def test_build_domain_check_row_structure():
    row = build_domain_check_row("example.com", "mx1.example.com", "✅ Found", "❌ Missing", "✅ Found")
    assert row["domain"] == "example.com"
    assert row["mx"] == "mx1.example.com"
    assert row["spf"] == "✅ Found"
    assert row["dkim"] == "❌ Missing"
    assert row["dmarc"] == "✅ Found"
    assert row["status"] == "⚠️ 2/3 Pass"
    assert row["tag"] == "partial"

