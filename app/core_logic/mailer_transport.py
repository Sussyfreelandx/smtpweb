import base64
import logging
import os
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

import requests

from app.core_logic.smtp_handler import SMTPHandler

log = logging.getLogger(__name__)


class BaseMailerTransport:
    def validate_configuration(self):
        return True, "Configured"

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None):
        raise NotImplementedError

    def disconnect(self):
        return

    def test_connection(self):
        return self.validate_configuration()


class SMTPMailerTransport(BaseMailerTransport):
    def __init__(self, config):
        self._handler = SMTPHandler(config)

    def validate_configuration(self):
        if not self._handler.smtp_server:
            return False, "SMTP server is missing"
        if not self._handler.username:
            return False, "SMTP username is missing"
        if not self._handler.password:
            return False, "SMTP password is missing"
        return True, "SMTP configured"

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None):
        return self._handler.send_email(
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            plain_content=plain_content,
            attachments=attachments,
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

    def _make_mime(self, to_email, subject, html_content, plain_content=None, attachments=None):
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['To'] = to_email
        msg['From'] = self.sender_email
        if self.reply_to_email:
            msg['Reply-To'] = self.reply_to_email

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

    def _send_google(self, to_email, subject, html_content, plain_content=None, attachments=None):
        endpoint = os.environ.get(
            'GOOGLE_MAILER_SEND_ENDPOINT',
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
        )
        mime = self._make_mime(to_email, subject, html_content, plain_content, attachments)
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

    def _send_microsoft(self, to_email, subject, html_content, plain_content=None, attachments=None):
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
        if self.reply_to_email:
            message['replyTo'] = [{'emailAddress': {'address': self.reply_to_email}}]

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
        if r.status_code in (200, 202):
            return True, "Sent via Microsoft Graph API"
        return False, f"Microsoft Graph error ({r.status_code}): {r.text[:200]}"

    def send_email(self, to_email, subject, html_content, plain_content=None, attachments=None):
        if self.provider == 'google':
            return self._send_google(to_email, subject, html_content, plain_content, attachments)
        if self.provider == 'microsoft':
            return self._send_microsoft(to_email, subject, html_content, plain_content, attachments)
        return False, f"Unsupported provider: {self.provider}"

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


def create_mailer_transport(config):
    transport = (config.get('transport') or 'smtp').lower()
    if transport == 'api':
        return APIMailerTransport(config)
    return SMTPMailerTransport(config)
