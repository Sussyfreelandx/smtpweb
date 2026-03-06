"""
Phase 4 extraction helpers from desktop codebase for domain checker behavior.
"""

from __future__ import annotations


def extract_domains_from_text(raw_text: str | None) -> list[str]:
    """
    Extract normalized domains from newline-separated emails/domains input.

    Behavior:
    - lowercases input
    - removes surrounding whitespace
    - accepts raw domains (must contain a dot, no spaces)
    - accepts emails and extracts their domain part
    - rejects malformed email rows (e.g. empty local/domain)
    - de-duplicates and returns sorted domains
    """
    domains: list[str] = []
    for line in (raw_text or "").splitlines():
        item = line.strip().lower()
        if not item or " " in item:
            continue
        if "@" in item:
            parts = item.split("@")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                continue
            item = parts[1]
        if "." not in item:
            continue
        domains.append(item)
    return sorted(set(domains))


def classify_auth_status(spf: str, dkim: str, dmarc: str) -> tuple[str, str]:
    """
    Return desktop-style domain auth status text and machine tag.
    """
    checks = [spf, dkim, dmarc]
    pass_count = sum(1 for c in checks if "✅" in (c or ""))
    if pass_count == 3:
        return "✅ All Pass", "pass"
    if pass_count == 0:
        return "❌ Critical Issues", "critical"
    return f"⚠️ {pass_count}/3 Pass", "partial"


def build_domain_check_row(domain: str, mx: str, spf: str, dkim: str, dmarc: str) -> dict:
    status, tag = classify_auth_status(spf, dkim, dmarc)
    return {
        "domain": domain,
        "mx": mx or "-",
        "spf": spf or "❌ Missing",
        "dkim": dkim or "❌ Missing",
        "dmarc": dmarc or "❌ Missing",
        "status": status,
        "tag": tag,
    }
