import smtplib
import ssl
import asyncio
import logging
import os
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from email.header import Header

try:
    import aiosmtp
    AIOSMTP_AVAILABLE = True
except ImportError:
    AIOSMTP_AVAILABLE = False

# Configure logging
log = logging.getLogger(__name__)

class SMTPHandler:
    """
    Handles all SMTP operations for the web application.
    This class is adapted from the original script and removes all UI dependencies.
    """

    def __init__(self, smtp_config):
        """
        Initializes the handler with SMTP settings.
        :param smtp_config: A dictionary with keys like 'server', 'port', 'username',
                            'password', 'use_tls', 'use_ssl', 'sender_name', 'sender_email'.
        """
        self.smtp_server = smtp_config.get('server')
        self.smtp_port = int(smtp_config.get('port', 587))
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username
        self.last_failure_reasons = {}

    def _create_secure_ssl_context(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def test_connection(self):
        """Tests the SMTP connection with the configured credentials."""
        if not self.smtp_server or not self.username or not self.password:
            return False, "SMTP configuration is incomplete."
        
        log.info(f"Testing SMTP connection to {self.smtp_server}:{self.smtp_port}...")
        try:
            context = self._create_secure_ssl_context()
            if self.use_ssl or self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=15) as server:
                    server.login(self.username, self.password)
            else:
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15) as server:
                    if self.use_tls:
                        server.starttls(context=context)
                    server.login(self.username, self.password)
            return True, "Connection successful!"
        except smtplib.SMTPAuthenticationError as e:
            error_msg = f"Authentication failed: {e.smtp_code} - {e.smtp_error.decode('utf-8', 'ignore') if isinstance(e.smtp_error, bytes) else e.smtp_error}"
            log.error(error_msg)
            return False, error_msg
        except Exception as e:
            log.error(f"Unexpected SMTP connection error: {e}")
            return False, f"An unexpected error occurred: {e}"

    def _html_to_text(self, html):
        try:
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br) *>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return text.strip()
        except Exception:
            return "HTML-only email. Please use a compatible client."

    def _create_mime_message(self, to_email, subject, html_content, attachments=None, unsubscribe_url=None):
        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = Header(subject, 'utf-8').encode()
        msg_root['From'] = formataddr((Header(self.sender_name, 'utf-8').encode(), self.sender_email))
        msg_root['To'] = to_email

        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')

        msg_alternative = MIMEMultipart('alternative')
        msg_root.attach(msg_alternative)
        msg_alternative.attach(MIMEText(self._html_to_text(html_content), 'plain', 'utf-8'))
        msg_alternative.attach(MIMEText(html_content, 'html', 'utf-8'))

        if attachments:
            for path in attachments:
                if os.path.exists(path):
                    try:
                        with open(path, "rb") as f:
                            part = MIMEBase('application', 'octet-stream')
                            part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(path)}"')
                        msg_root.attach(part)
                    except Exception as e:
                        log.warning(f"Could not attach file {path}: {e}")
        
        return msg_root

    async def send_email_async(self, to_email, subject, html_content, attachments=None, unsubscribe_url=None):
        """Sends a single email asynchronously using aiosmtp."""
        if not AIOSMTP_AVAILABLE:
            raise RuntimeError("aiosmtp is not installed. Cannot send email asynchronously.")

        mime_message = self._create_mime_message(to_email, subject, html_content, attachments, unsubscribe_url)
        
        context = self._create_secure_ssl_context()
        use_ssl = self.use_ssl or self.smtp_port == 465
        use_tls_starttls = self.use_tls and not use_ssl

        try:
            log.info(f"Connecting to {self.smtp_server}:{self.smtp_port} for {to_email}")
            async with aiosmtp.SMTP(
                hostname=self.smtp_server,
                port=self.smtp_port,
                use_tls=use_ssl,
                tls_context=context,
                timeout=30
            ) as smtp_client:
                if use_tls_starttls:
                    await smtp_client.starttls(tls_context=context)
                
                await smtp_client.login(self.username, self.password)
                await smtp_client.send_message(mime_message)
            
            log.info(f"Successfully sent email to {to_email} via async SMTP.")
            return True, "Sent"
        except aiosmtp.errors.SMTPAuthenticationError as e:
            log.error(f"Async SMTP Authentication Error for {to_email}: {e.code} {e.message}")
            return False, f"SMTP Auth Error: {e.code} {e.message}"
        except asyncio.TimeoutError:
            log.error(f"Async SMTP connection to {self.smtp_server} timed out for {to_email}.")
            return False, "Connection Timed Out"
        except Exception as e:
            log.error(f"Async SMTP sending failed for {to_email}: {e}")
            return False, str(e)