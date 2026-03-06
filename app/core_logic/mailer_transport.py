import base64
import logging
import os
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

import requests

from app.core_logic.desktop_core_compat import detect_tls_mode, universal_smtp_send
from app.core_logic.desktop_direct_mx_compat import send_via_direct_mx
from app.core_logic.smtp_handler import SMTPHandler

log = logging.getLogger(__name__)


def _is_truthy_env(value):
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _normalize_recipient_list(value):
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = str(value).replace('\n', ',').split(',')
    normalized = []
    for item in candidates:
        addr = str(item).strip()
        if addr:
            normalized.append(addr)
    return normalized


class BaseMailerTransport:
    def validate_configuration(self):
        return True, "Configured"

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None, **kwargs):
        raise NotImplementedError

    def disconnect(self):
        return

    def test_connection(self):
        return self.validate_configuration()


class SMTPMailerTransport(BaseMailerTransport):
    def __init__(self, config):
        self._handler = SMTPHandler(config)
        self._allow_insecure_ssl = bool(config.get('allow_insecure_ssl', False))
        self._default_envelope_from = config.get('envelope_from')
        self._default_in_reply_to = config.get('in_reply_to')
        self._default_references = config.get('references')
        self._default_cc_emails = _normalize_recipient_list(config.get('cc_emails'))
        self._default_bcc_emails = _normalize_recipient_list(config.get('bcc_emails'))

    def validate_configuration(self):
        if not self._handler.smtp_server:
            return False, "SMTP server is missing"
        if not self._handler.username:
            return False, "SMTP username is missing"
        if not self._handler.password:
            return False, "SMTP password is missing"
        return True, "SMTP configured"

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None, **kwargs):
        envelope_from = kwargs.get('envelope_from', self._default_envelope_from)
        in_reply_to = kwargs.get('in_reply_to', self._default_in_reply_to)
        references = kwargs.get('references', self._default_references)
        allow_insecure_ssl = bool(kwargs.get('allow_insecure_ssl', self._allow_insecure_ssl))
        cc_emails = _normalize_recipient_list(kwargs.get('cc_emails', self._default_cc_emails))
        bcc_emails = _normalize_recipient_list(kwargs.get('bcc_emails', self._default_bcc_emails))

        custom_headers = kwargs.get('custom_headers')

        mime_message = self._handler.create_mime_message(
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            plain_content=plain_content,
            attachments=attachments,
            custom_headers=custom_headers,
            cc_emails=cc_emails,
        )

        inferred_ssl, inferred_starttls = detect_tls_mode(
            int(self._handler.smtp_port or 587),
            ssl_enabled=bool(self._handler.use_ssl),
        )
        use_ssl = bool(self._handler.use_ssl) or inferred_ssl
        use_starttls = (not use_ssl) and (bool(self._handler.use_tls) or inferred_starttls)

        return universal_smtp_send(
            host=self._handler.smtp_server,
            port=int(self._handler.smtp_port or 587),
            username=self._handler.username,
            password=self._handler.password,
            message=mime_message,
            use_ssl=use_ssl,
            use_starttls=use_starttls,
            timeout=30,
            allow_insecure_ssl=allow_insecure_ssl,
            envelope_from=envelope_from,
            in_reply_to=in_reply_to,
            references=references,
            additional_recipients=bcc_emails,
        )

    def disconnect(self):
        self._handler.disconnect()

    def test_connection(self):
        return self._handler.test_connection()


class APIMailerTransport(BaseMailerTransport):
    def __init__(self, config):
        self.provider = (config.get('provider') or '').lower()
        self.sender_email = config.get('sender_email') or config.get('username')
        self.access_token = config.get('password')
        self.reply_to_email = config.get('reply_to_email')
        self.default_cc_emails = _normalize_recipient_list(config.get('cc_emails'))
        self.default_bcc_emails = _normalize_recipient_list(config.get('bcc_emails'))

    def _proxy_config(self):
        proxy_url = os.environ.get('MAILER_PROXY_URL')
        if proxy_url:
            return {'http': proxy_url, 'https': proxy_url}
        http_proxy = os.environ.get('HTTP_PROXY')
        https_proxy = os.environ.get('HTTPS_PROXY')
        proxies = {}
        if http_proxy:
            proxies['http'] = http_proxy
        if https_proxy:
            proxies['https'] = https_proxy
        return proxies or None

    def validate_configuration(self):
        if self.provider not in {'google', 'microsoft'}:
            return False, "API provider must be 'google' or 'microsoft'"
        if not self.access_token:
            return False, "API access token is missing"
        if not self.sender_email:
            return False, "Sender email is missing"
        return True, (
            f"{self.provider.title()} API configured. "
            "Google/Microsoft send from their own infrastructure."
        )

    def _make_mime(self, to_email, subject, html_content, plain_content=None, attachments=None, cc_emails=None, bcc_emails=None, custom_headers=None):
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['To'] = to_email
        if cc_emails:
            msg['Cc'] = ', '.join(cc_emails)
        if bcc_emails:
            msg['Bcc'] = ', '.join(bcc_emails)
        msg['From'] = self.sender_email
        if self.reply_to_email:
            msg['Reply-To'] = self.reply_to_email
        if custom_headers:
            for key, value in custom_headers.items():
                msg[key] = str(value)

        alt = MIMEMultipart('alternative')
        # Plain part first, then HTML, so email clients can prefer richer HTML rendering.
        alt.attach(MIMEText(plain_content or "", 'plain', 'utf-8'))
        alt.attach(MIMEText(html_content or "", 'html', 'utf-8'))
        msg.attach(alt)

        for filepath in attachments or []:
            if not os.path.exists(filepath):
                continue
            with open(filepath, 'rb') as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filepath)}"')
            msg.attach(part)

        return msg

    def _send_google(self, to_email, subject, html_content, plain_content=None, attachments=None, cc_emails=None, bcc_emails=None):
        return self._send_google_with_headers(
            to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, None
        )

    def _send_microsoft(self, to_email, subject, html_content, plain_content=None, attachments=None, cc_emails=None, bcc_emails=None):
        return self._send_microsoft_with_headers(
            to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, None
        )

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None, **kwargs):
        cc_emails = _normalize_recipient_list(kwargs.get('cc_emails', self.default_cc_emails))
        bcc_emails = _normalize_recipient_list(kwargs.get('bcc_emails', self.default_bcc_emails))
        custom_headers = kwargs.get('custom_headers')
        if self.provider == 'google':
            return self._send_google_with_headers(
                to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, custom_headers
            )
        if self.provider == 'microsoft':
            return self._send_microsoft_with_headers(
                to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, custom_headers
            )
        return False, f"Unsupported provider: {self.provider}"

    def _send_google_with_headers(self, to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, custom_headers):
        endpoint = os.environ.get(
            'GOOGLE_MAILER_SEND_ENDPOINT',
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
        )
        mime = self._make_mime(
            to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, custom_headers
        )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        r = requests.post(
            endpoint,
            headers={'Authorization': f'Bearer {self.access_token}'},
            json={'raw': raw},
            timeout=30,
            proxies=self._proxy_config(),
        )
        if 200 <= r.status_code < 300:
            return True, "Sent via Google API"
        return False, f"Google API error ({r.status_code}): {r.text[:200]}"

    def _send_microsoft_with_headers(self, to_email, subject, html_content, plain_content, attachments, cc_emails, bcc_emails, custom_headers):
        endpoint = os.environ.get(
            'MICROSOFT_MAILER_SEND_ENDPOINT',
            'https://graph.microsoft.com/v1.0/me/sendMail'
        )
        body_content = html_content or plain_content or ''
        body_content_type = 'HTML' if html_content else 'Text'
        message = {
            'subject': subject,
            'body': {'contentType': body_content_type, 'content': body_content},
            'toRecipients': [{'emailAddress': {'address': to_email}}],
            'from': {'emailAddress': {'address': self.sender_email}},
        }
        if cc_emails:
            message['ccRecipients'] = [{'emailAddress': {'address': addr}} for addr in cc_emails]
        if bcc_emails:
            message['bccRecipients'] = [{'emailAddress': {'address': addr}} for addr in bcc_emails]
        if self.reply_to_email:
            message['replyTo'] = [{'emailAddress': {'address': self.reply_to_email}}]
        if custom_headers:
            message['internetMessageHeaders'] = [{'name': str(k), 'value': str(v)} for k, v in custom_headers.items()]

        graph_attachments = []
        for filepath in attachments or []:
            if not os.path.exists(filepath):
                continue
            with open(filepath, 'rb') as f:
                content = base64.b64encode(f.read()).decode()
            graph_attachments.append({
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': os.path.basename(filepath),
                'contentType': 'application/octet-stream',
                'contentBytes': content
            })
        if graph_attachments:
            message['attachments'] = graph_attachments

        r = requests.post(
            endpoint,
            headers={'Authorization': f'Bearer {self.access_token}', 'Content-Type': 'application/json'},
            json={'message': message, 'saveToSentItems': True},
            timeout=30,
            proxies=self._proxy_config(),
        )
        if 200 <= r.status_code < 300:
            return True, "Sent via Microsoft Graph API"
        return False, f"Microsoft Graph error ({r.status_code}): {r.text[:200]}"

    def test_connection(self):
        ok, msg = self.validate_configuration()
        if not ok:
            return ok, msg

        if self.provider == 'google':
            endpoint = os.environ.get(
                'GOOGLE_MAILER_PROFILE_ENDPOINT',
                'https://gmail.googleapis.com/gmail/v1/users/me/profile'
            )
        else:
            endpoint = os.environ.get(
                'MICROSOFT_MAILER_PROFILE_ENDPOINT',
                'https://graph.microsoft.com/v1.0/me'
            )

        try:
            r = requests.get(
                endpoint,
                headers={'Authorization': f'Bearer {self.access_token}'},
                timeout=20,
                proxies=self._proxy_config(),
            )
            if 200 <= r.status_code < 300:
                return True, (
                    f"{self.provider.title()} API connection successful. "
                    "Google/Microsoft send from their own infrastructure."
                )
            return False, f"{self.provider.title()} API auth failed ({r.status_code}): {r.text[:200]}"
        except Exception as e:
            return False, f"{self.provider.title()} API connection error: {e}"


class DirectMXMailerTransport(BaseMailerTransport):
    def __init__(self, config):
        self.sender_name = config.get('sender_name') or 'Sender'
        self.sender_email = config.get('sender_email') or config.get('username')
        self.reply_to_email = config.get('reply_to_email')
        self.allow_insecure_ssl = bool(config.get('allow_insecure_ssl', False))
        self.default_cc_emails = _normalize_recipient_list(config.get('cc_emails'))
        self.default_bcc_emails = _normalize_recipient_list(config.get('bcc_emails'))

    def validate_configuration(self):
        if not self.sender_email or '@' not in self.sender_email:
            return False, "Direct MX sender email is missing or invalid"
        return True, "Direct MX configured"

    def _make_mime(self, to_email, subject, html_content, plain_content=None, attachments=None, cc_emails=None, custom_headers=None):
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['To'] = to_email
        if cc_emails:
            msg['Cc'] = ', '.join(cc_emails)
        msg['From'] = self.sender_email
        if self.reply_to_email:
            msg['Reply-To'] = self.reply_to_email
        if custom_headers:
            for key, value in custom_headers.items():
                msg[key] = str(value)

        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(plain_content or "", 'plain', 'utf-8'))
        alt.attach(MIMEText(html_content or "", 'html', 'utf-8'))
        msg.attach(alt)

        for filepath in attachments or []:
            if not os.path.exists(filepath):
                continue
            with open(filepath, 'rb') as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filepath)}"')
            msg.attach(part)
        return msg

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None, **kwargs):
        envelope_from = kwargs.get('envelope_from')
        cc_emails = _normalize_recipient_list(kwargs.get('cc_emails', self.default_cc_emails))
        bcc_emails = _normalize_recipient_list(kwargs.get('bcc_emails', self.default_bcc_emails))
        custom_headers = kwargs.get('custom_headers')
        message = self._make_mime(
            to_email, subject, html_content, plain_content, attachments, cc_emails=cc_emails, custom_headers=custom_headers
        )
        if kwargs.get('in_reply_to'):
            message['In-Reply-To'] = kwargs['in_reply_to']
        if kwargs.get('references'):
            message['References'] = kwargs['references']
        return send_via_direct_mx(
            message=message,
            to_email=to_email,
            envelope_from=envelope_from,
            timeout=30,
            additional_recipients=bcc_emails,
            # Insecure SSL for direct MX is gated by env to avoid accidental production misuse.
            allow_insecure_ssl=(
                bool(kwargs.get('allow_insecure_ssl', self.allow_insecure_ssl))
                and _is_truthy_env(os.environ.get('ALLOW_INSECURE_DIRECT_MX', ''))
            ),
        )


def create_mailer_transport(config):
    transport = (config.get('transport') or 'smtp').lower()
    if transport == 'api':
        return APIMailerTransport(config)
    if transport == 'direct_mx':
        return DirectMXMailerTransport(config)
    return SMTPMailerTransport(config)
