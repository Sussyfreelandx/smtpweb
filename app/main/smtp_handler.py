import smtplib
import ssl
import logging
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header

# aiosmtp has been completely removed from this file.

# Configure logging
log = logging.getLogger(__name__)

class SMTPHandler:
    """
    Handles all SMTP operations for the web application.
    This class has been updated to use the standard smtplib for sending.
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

    def _create_secure_ssl_context(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _html_to_text(self, html):
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</(p|h[1-6]|li|div|tr|br)\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        return text.strip()

    def _create_mime_message(self, to_email, subject, html_content, attachments=None, unsubscribe_url=None):
        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = Header(subject, 'utf-8')
        msg_root['From'] = formataddr((str(Header(self.sender_name, 'utf-8')), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid()

        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')

        msg_alternative = MIMEMultipart('alternative')
        msg_root.attach(msg_alternative)
        msg_alternative.attach(MIMEText(self._html_to_text(html_content), 'plain', 'utf-8'))
        msg_alternative.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        return msg_root

    def send_email_sync(self, to_email, subject, html_content, unsubscribe_url=None):
        """
        Sends a single email synchronously using smtplib.
        This is the new primary sending method.
        """
        if not all([self.smtp_server, self.username, self.password]):
            log.error("SMTP sending failed: configuration is incomplete.")
            return False, "SMTP configuration is incomplete."
            
        mime_message = self._create_mime_message(to_email, subject, html_content, unsubscribe_url=unsubscribe_url)
        context = self._create_secure_ssl_context()
        
        try:
            use_ssl = self.use_ssl or self.smtp_port == 465
            if use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)

            with server:
                if not use_ssl and self.use_tls:
                    server.starttls(context=context)
                server.login(self.username, self.password)
                server.send_message(mime_message)

            log.info(f"Successfully sent email to {to_email} via smtplib.")
            return True, "Sent"
        except smtplib.SMTPAuthenticationError as e:
            error_msg = f"SMTP Auth Error: {e.smtp_code} {e.smtp_error.decode('utf-8', 'ignore') if e.smtp_error else ''}"
            log.error(f"SMTP Auth Error for {to_email}: {error_msg}")
            return False, error_msg
        except Exception as e:
            log.error(f"SMTP sending failed for {to_email}: {e}")
            return False, str(e)
