import smtplib
import ssl
import logging
import re
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
import os

log = logging.getLogger(__name__)


class SMTPHandler:
    """Handles all SMTP operations."""
    
    def __init__(self, smtp_config):
        self.smtp_server = smtp_config.get('server')
        self.smtp_port = int(smtp_config.get('port', 587))
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username
        self.reply_to_email = smtp_config.get('reply_to_email')
        
        # Connection management
        self._connection = None
        self._connection_time = None
        self._max_connection_age = 300  # 5 minutes
        
        # Retry configuration
        self.max_retries = 3
        self.retry_delay = 5
    
    def _create_secure_ssl_context(self):
        """Create a secure SSL context."""
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context
    
    def _html_to_text(self, html):
        """Convert HTML to plain text."""
        if not html:
            return ""
        
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\s+', ' ', text)
        
        lines = [line.strip() for line in text.split('\n')]
        return '\n'.join(line for line in lines if line).strip()
    
    def _create_mime_message(self, to_email, subject, html_content, plain_content=None,
                             unsubscribe_url=None, attachments=None, custom_headers=None):
        """Create a MIME message."""
        if attachments:
            msg_root = MIMEMultipart('mixed')
            msg_alt = MIMEMultipart('alternative')
            msg_root.attach(msg_alt)
        else:
            msg_root = MIMEMultipart('alternative')
            msg_alt = msg_root
        
        # Required headers
        msg_root['Subject'] = Header(subject, 'utf-8')
        msg_root['From'] = formataddr((str(Header(self.sender_name, 'utf-8')), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid()
        msg_root['MIME-Version'] = '1.0'
        
        # Reply-To header
        if self.reply_to_email:
            msg_root['Reply-To'] = self.reply_to_email
        
        # List-Unsubscribe headers
        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')
        
        # Custom headers
        if custom_headers:
            for key, value in custom_headers.items():
                msg_root.add_header(key, value)
        
        # Message body
        if not plain_content:
            plain_content = self._html_to_text(html_content)
        
        part_plain = MIMEText(plain_content, 'plain', 'utf-8')
        part_html = MIMEText(html_content, 'html', 'utf-8')
        
        msg_alt.attach(part_plain)
        msg_alt.attach(part_html)
        
        # Attachments
        if attachments:
            for filepath in attachments:
                if os.path.exists(filepath):
                    try:
                        with open(filepath, 'rb') as f:
                            part = MIMEBase('application', 'octet-stream')
                            part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header(
                            'Content-Disposition',
                            f'attachment; filename="{os.path.basename(filepath)}"'
                        )
                        msg_root.attach(part)
                    except Exception as e:
                        log.error(f"Failed to attach {filepath}: {e}")
        
        return msg_root
    
    def connect(self):
        """Establish connection to SMTP server."""
        try:
            context = self._create_secure_ssl_context()
            
            if self.use_ssl or self.smtp_port == 465:
                self._connection = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context)
            else:
                self._connection = smtplib.SMTP(self.smtp_server, self.smtp_port)
                if self.use_tls:
                    self._connection.starttls(context=context)
            
            self._connection.login(self.username, self.password)
            self._connection_time = time.time()
            return True, "Connected"
        except Exception as e:
            log.error(f"SMTP Connection Error: {e}")
            return False, str(e)

    def quit(self):
        """Close connection."""
        if self._connection:
            try:
                self._connection.quit()
            except:
                pass
            self._connection = None

    def send_email(self, to_email, subject, html_content, plain_content=None,
                   unsubscribe_url=None, attachments=None, custom_headers=None):
        """Send an email."""
        msg = self._create_mime_message(
            to_email, subject, html_content, plain_content,
            unsubscribe_url, attachments, custom_headers
        )
        
        for attempt in range(self.max_retries):
            try:
                if not self._connection:
                    success, error = self.connect()
                    if not success:
                        raise Exception(f"Connection failed: {error}")
                
                self._connection.sendmail(self.sender_email, to_email, msg.as_string())
                return True, "Sent"
            
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                self._connection = None  # Force reconnect
                time.sleep(self.retry_delay)
            except Exception as e:
                log.error(f"Send Error: {e}")
                return False, str(e)
        
        return False, "Max retries exceeded"

    def test_connection(self):
        """Test if credentials are valid."""
        success, msg = self.connect()
        self.quit()
        return success, msg
