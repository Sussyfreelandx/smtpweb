import smtplib
import ssl
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header

# Configure logging
log = logging.getLogger(__name__)

class SMTPHandler:
    """
    Handles all SMTP operations using the robust, synchronous smtplib library.
    """
    def __init__(self, smtp_config):
        self.smtp_server = smtp_config.get('server')
        self.smtp_port = int(smtp_config.get('port', 587))
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username

    def _create_secure_ssl_context(self):
        return ssl.create_default_context()

    def _html_to_text(self, html):
        import re
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</(p|h[1-6]|li|div|tr|br)\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        return text.strip()

    def _create_mime_message(self, to_email, subject, html_content, unsubscribe_url=None):
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

    def send_email(self, to_email, subject, html_content, unsubscribe_url=None):
        """Sends a single email using synchronous smtplib."""
        mime_message = self._create_mime_message(to_email, subject, html_content, unsubscribe_url=unsubscribe_url)
        context = self._create_secure_ssl_context()
        
        try:
            if self.use_ssl or self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30) as server:
                    server.login(self.username, self.password)
                    server.send_message(mime_message)
            else:
                with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
                    if self.use_tls:
                        server.starttls(context=context)
                    server.login(self.username, self.password)
                    server.send_message(mime_message)
            
            log.info(f"Successfully sent email to {to_email} via smtplib.")
            return True, "Sent"
        except smtplib.SMTPAuthenticationError as e:
            msg = f"SMTP Auth Error: {e.smtp_code} {e.smtp_error}"
            log.error(f"smtplib auth error for {to_email}: {msg}")
            return False, msg
        except Exception as e:
            log.error(f"smtplib sending failed for {to_email}: {e}")
            return False, str(e)
