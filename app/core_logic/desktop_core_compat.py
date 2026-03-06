"""
Phase 1 extraction helpers from desktop codebase.

This module intentionally contains backend-only logic migrated from
`paris_sender_complete1.py` so it can be reused in web/Celery flows.
"""

from email.utils import getaddresses, parseaddr
import smtplib
import ssl


def detect_tls_mode(port, ssl_enabled=False):
    """
    Determine SMTP security mode from port.

    Returns tuple: (use_ssl, use_starttls)
    """
    if port == 465 or ssl_enabled:
        return True, False
    if port in (587, 2525):
        return False, True
    return False, False


def universal_smtp_send(
    host,
    port,
    username,
    password,
    message,
    use_ssl=False,
    use_starttls=False,
    timeout=30,
    allow_insecure_ssl=False,
    envelope_from=None,
    in_reply_to=None,
    references=None,
    additional_recipients=None,
):
    """
    Universal SMTP sender logic extracted from desktop app.
    """

    # Optional reply-chain injection
    if in_reply_to:
        if message.get('In-Reply-To'):
            message.replace_header('In-Reply-To', in_reply_to)
        else:
            message['In-Reply-To'] = in_reply_to
    if references:
        if message.get('References'):
            message.replace_header('References', references)
        else:
            message['References'] = references
    subject = message.get('Subject')
    if in_reply_to and subject and not str(subject).lower().startswith('re:'):
        try:
            message.replace_header('Subject', f"Re: {subject}")
        except KeyError:
            message['Subject'] = f"Re: {subject}"

    def _build_ssl_context():
        ctx = ssl.create_default_context()
        if allow_insecure_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _attempt_send(starttls_enabled):
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=_build_ssl_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()

        if not use_ssl and starttls_enabled and server.has_extn('STARTTLS'):
            context = _build_ssl_context()
            server.starttls(context=context)
            server.ehlo()

        if username and password:
            login_user = _resolve_login_user(username, message)
            server.login(login_user, str(password).rstrip('\r\n'))

        if envelope_from:
            # Bcc header is intentionally excluded to avoid leaking hidden recipients in raw MIME;
            # hidden recipients are supplied via `additional_recipients`.
            to_fields = (message.get_all('To', []) or []) + (message.get_all('Cc', []) or [])
            recipients = [addr for _, addr in getaddresses(to_fields) if addr]
            if additional_recipients:
                recipients.extend([addr for addr in additional_recipients if addr])
            if not recipients:
                to_addr = parseaddr(message.get('To', '') or '')[1]
                recipients = [to_addr] if to_addr else []
            server.sendmail(envelope_from, recipients, message.as_string())
        else:
            # Hidden recipients are delivered through `additional_recipients` for send_message path as well.
            to_fields = (message.get_all('To', []) or []) + (message.get_all('Cc', []) or [])
            recipients = [addr for _, addr in getaddresses(to_fields) if addr]
            if additional_recipients:
                recipients.extend([addr for addr in additional_recipients if addr])
            server.send_message(message, to_addrs=recipients or None)
        server.quit()

    def _resolve_login_user(raw_username, mime_message):
        username_str = str(raw_username).strip()
        login_user = (parseaddr(username_str)[1] or username_str)
        if login_user and '@' not in login_user:
            sender_or_from = mime_message.get('Sender') or mime_message.get('From') or ''
            from_addr = parseaddr(str(sender_or_from).strip())[1]
            if from_addr and '@' in from_addr:
                login_user = from_addr
        return login_user

    try:
        _attempt_send(starttls_enabled=use_starttls)
        return True, "Sent successfully"
    except smtplib.SMTPServerDisconnected as e:
        if use_starttls and not use_ssl:
            try:
                _attempt_send(starttls_enabled=False)
                return True, "Sent successfully (STARTTLS fallback)"
            except Exception as retry_e:
                return False, f"Connection unexpectedly closed (STARTTLS fallback also failed): {retry_e}"
        return False, f"Connection unexpectedly closed: {e}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Authentication failed: {e}"
    except smtplib.SMTPConnectError as e:
        return False, f"Connection failed: {e}"
    except ssl.SSLError as e:
        return False, f"SSL error: {e}"
    except Exception as e:
        return False, f"SMTP error: {e}"
