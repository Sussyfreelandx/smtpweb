from app.core_logic.desktop_content_compat import (
    build_context_preview_from_email,
    validate_template_content,
)


def test_validate_template_content_detects_unknown_placeholder():
    result = validate_template_content("Hello [UNKNOWNFIELD] {{ firstname }}")
    assert result["ok"] is True
    assert any("Unknown bracket placeholders" in w for w in result["warnings"])
    assert "firstname" in result["jinja_vars"]


def test_validate_template_content_detects_unclosed_expression():
    result = validate_template_content("Hello {{ firstname")
    assert result["ok"] is False
    assert any("Jinja2 syntax error" in e for e in result["errors"])
    mixed_result = validate_template_content("Hello {{ firstname }} and {{ company")
    assert mixed_result["ok"] is False
    assert any("Jinja2 syntax error" in e for e in mixed_result["errors"])
    assert "firstname" in mixed_result["jinja_vars"]


def test_build_context_preview_from_email():
    ctx = build_context_preview_from_email("john.doe@example.com")
    assert ctx["firstname"] == "John"
    assert ctx["company"] == "Example"
    assert ctx["domain"] == "example.com"


def test_build_context_preview_with_invalid_email():
    ctx = build_context_preview_from_email("invalid")
    assert ctx["firstname"] == "User"
    assert ctx["company"] == "your company"
    assert ctx["domain"] == ""


def test_build_context_preview_rejects_empty_local_or_domain():
    local_empty = build_context_preview_from_email("@example.com")
    assert local_empty["firstname"] == "User"
    assert local_empty["company"] == "your company"
    assert local_empty["domain"] == ""

    domain_empty = build_context_preview_from_email("user@")
    assert domain_empty["firstname"] == "User"
    assert domain_empty["company"] == "your company"
    assert domain_empty["domain"] == ""
