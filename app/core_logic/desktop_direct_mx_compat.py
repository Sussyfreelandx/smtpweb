"""
Phase 6 compatibility helpers for desktop-style direct-to-MX delivery.
"""

from __future__ import annotations

from email.utils import getaddresses, parseaddr
import os
import re
import smtplib
import socket
import ssl

try:
    import dns.resolver
    DNSPYTHON_AVAILABLE = True
except ImportError:
    dns = None
    DNSPYTHON_AVAILABLE = False

DOMAIN_VALIDATION_PATTERN = (
    r'^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?'
    r'(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*'
    r'\.[A-Za-z]{2,}$'
)


def resolve_mx_hosts(domain: str) -> list[str]:
    """
    Resolve MX hosts ordered by preference; fallback to domain when unavailable.
    """
    if not domain:
        return []
    if not DNSPYTHON_AVAILABLE:
        return [domain]
    try:
        answers = dns.resolver.resolve(domain, 'MX')
        sorted_answers = sorted(answers, key=lambda r: r.preference)
        hosts = [str(r.exchange).rstrip('.') for r in sorted_answers]
        return hosts or [domain]
    except Exception:
        return [domain]


def send_via_direct_mx(
    message,
    to_email: str,
    envelope_from: str | None = None,
    timeout: int = 30,
    allow_insecure_ssl: bool = False,
    additional_recipients: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Deliver message directly to recipient domain MX hosts (no SMTP auth).
    """
    if '@' in (to_email or ''):
        recipient_domain = to_email.split('@', 1)[1].lower().strip()
    else:
        recipient_domain = ''
    if not recipient_domain:
        return False, "Invalid recipient domain for direct MX"

    mx_hosts = resolve_mx_hosts(recipient_domain)
    if not mx_hosts:
        return False, "No MX host resolved"

    to_fields = message.get_all('To', []) + message.get_all('Cc', []) + message.get_all('Bcc', [])
    recipients = [addr for _, addr in getaddresses(to_fields) if addr and '@' in addr] or [to_email]
    if additional_recipients:
        recipients.extend([addr for addr in additional_recipients if addr and '@' in addr])
    # Ensure Bcc header is not included in serialized message payload.
    while message.get('Bcc') is not None:
        del message['Bcc']

    sender = envelope_from or parseaddr(message.get('From', '') or '')[1]
    if not sender:
        fallback_domain = os.environ.get('DIRECT_MX_FALLBACK_DOMAIN', '')
        if not fallback_domain:
            return False, "DIRECT_MX_FALLBACK_DOMAIN environment variable must be set when sender is missing"
        if not re.fullmatch(DOMAIN_VALIDATION_PATTERN, fallback_domain):
            return False, "Invalid DIRECT_MX_FALLBACK_DOMAIN format"
        sender = f"postmaster@{fallback_domain}"

    def _ssl_context():
        context = ssl.create_default_context()
        if allow_insecure_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    last_error = None
    host_errors = {}
    attempted_hosts = []
    for mx_host in mx_hosts:
        attempted_hosts.append(mx_host)
        try:
            server = smtplib.SMTP(mx_host, 25, timeout=timeout)
            server.ehlo(socket.getfqdn())
            if server.has_extn('STARTTLS'):
                server.starttls(context=_ssl_context())
                server.ehlo(socket.getfqdn())
            server.sendmail(sender, recipients, message.as_string())
            server.quit()
            return True, f"Sent via direct MX ({mx_host})"
        except Exception as e:
            last_error = e
            host_errors[mx_host] = str(e)
            continue

    host_error_text = "; ".join(f"{host}: {err}" for host, err in host_errors.items())
    return False, f"Direct MX failed after hosts [{', '.join(attempted_hosts)}]: {last_error}. Details: {host_error_text}"
