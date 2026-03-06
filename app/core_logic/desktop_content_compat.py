"""
Phase 3 extraction helpers from desktop codebase.

This module contains reusable content/template validation logic adapted from
desktop behavior so web routes can provide equivalent checks.
"""

import re

try:
    from jinja2 import Environment, exceptions as jinja_exceptions
    JINJA2_AVAILABLE = True
except ImportError:
    Environment = None
    jinja_exceptions = None
    JINJA2_AVAILABLE = False


KNOWN_BRACKET_PLACEHOLDERS = {
    "[EMAIL]", "[FIRSTNAME]", "[LASTNAME]", "[COMPANY]",
    "[DATE]", "[TIME]", "[GREETINGS]", "[SENDER_NAME]",
    "[DOMAIN]", "[UNAME]", "[EMAIL64]", "[COMPANYFULL]",
    "[DATE-1DAY]", "[DATE-2]", "[FUTURE-1DAY]", "[CURRENTDATE]",
}

JINJA_VAR_PATTERN = r'\{\{\s*(\w+(?:\.\w+)*)\s*\}\}'


def validate_template_content(content):
    """
    Validate content for placeholder/template issues.

    Returns: dict with keys `ok`, `errors`, `warnings`, `jinja_vars`.
    """
    errors = []
    warnings = []
    jinja_vars = []

    content = content or ""

    bracket_matches = re.findall(r'\[([A-Z0-9_\-]+)\]', content)
    unknown = [f"[{m}]" for m in bracket_matches if f"[{m}]" not in KNOWN_BRACKET_PLACEHOLDERS]
    if unknown:
        warnings.append(f"Unknown bracket placeholders: {', '.join(sorted(set(unknown)))}")

    if JINJA2_AVAILABLE:
        try:
            env = Environment()
            env.parse(content)
            jinja_vars = re.findall(JINJA_VAR_PATTERN, content)
        except jinja_exceptions.TemplateSyntaxError as e:
            errors.append(f"Jinja2 syntax error: {e}")
            jinja_vars = re.findall(JINJA_VAR_PATTERN, content)
    else:
        jinja_vars = re.findall(JINJA_VAR_PATTERN, content)

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "jinja_vars": sorted(set(jinja_vars)),
    }


def build_context_preview_from_email(email):
    """
    Build simple context preview from recipient email.

    Note: uses a pragmatic parser (`local@domain` split) intended for campaign
    preview UX, not full RFC 5321 mailbox parsing.
    """
    if not email or email.count('@') != 1:
        return {
            "email": email or "",
            "firstname": "User",
            "company": "your company",
            "domain": "",
        }

    local_part, domain = email.split('@', 1)
    if not local_part or not domain:
        return {
            "email": email,
            "firstname": "User",
            "company": "your company",
            "domain": "",
        }
    parts = re.split(r'[._\-+]+', local_part)
    valid_parts = [p for p in parts if len(p) >= 1 and p.isalpha()]
    firstname = valid_parts[0].capitalize() if valid_parts else "User"
    company = domain.split('.')[0].capitalize()

    return {
        "email": email,
        "firstname": firstname,
        "company": company,
        "domain": domain,
    }
