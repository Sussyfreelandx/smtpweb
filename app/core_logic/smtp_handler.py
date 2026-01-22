import smtplib
import ssl
import logging
import re
import os
import socks  # pip install PySocks
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
from datetime import datetime, timedelta
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

log = logging.getLogger(__name__)

# --- PROXY SETTINGS ---
# These are read from the environment once, when the module is loaded.
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

class ProxySMTP(smtplib.SMTP):
    """
    Custom SMTP class that uses a SOCKS5 proxy for the connection
    without patching the global socket module. This is thread-safe.
    """
    def _get_socket(self, host, port, timeout):
        if PROXY_HOST: 
            log.debug(f"Connecting to {host}:{port} via proxy {PROXY_HOST}:{PROXY_PORT}")
            return socks.create_connection(
                (host, port),
                timeout=timeout,
                proxy_type=socks.SOCKS5,
                proxy_addr=PROXY_HOST,
                proxy_port=PROXY_PORT,
                proxy_username=PROXY_USER,
                proxy_password=PROXY_PASS
            )
        else:
            # Fallback to standard socket connection if no proxy is configured
            return socket.create_connection((host, port), timeout)

class ProxySMTP_SSL(smtplib.SMTP_SSL):
    """
    Custom SMTP_SSL class that uses a SOCKS5 proxy for the connection.
    This is thread-safe and avoids global patching.
    """
    def _get_socket(self, host, port, timeout):
        if PROXY_HOST:
            # 1. Connect to Proxy -> Target via plain TCP first
            log.debug(f"Connecting (SSL) to {host}:{port} via proxy {PROXY_HOST}:{PROXY_PORT}")
            sock = socks.create_connection(
                (host, port),
                timeout=timeout,
                proxy_type=socks.SOCKS5,
                proxy_addr=PROXY_HOST,
                proxy_port=PROXY_PORT,
                proxy_username=PROXY_USER,
                proxy_password=PROXY_PASS
            )
            # 2. Wrap the established socket with SSL for encryption
            new_socket = self.context.wrap_socket(sock, server_hostname=host)
            return new_socket
        else: 
            # Fallback to standard SSL socket connection if no proxy is configured
            return super()._get_socket(host, port, timeout)

class SMTPHandler:
    """
    Robust SMTP Handler supporting connection pooling, multi-threaded sending,
    warmup, and safe SOCKS5 Proxying via custom SMTP classes.
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
        self.reply_to_email = smtp_config.get('reply_to_email')
        
        self._lock = threading.Lock()
        self._recent_sends = deque(maxlen=1000)
    
    def _create_secure_ssl_context(self):
        """Create a secure SSL context for TLS/SSL connections."""
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _html_to_text(self, html):
        """Convert HTML to a reasonable plain text version."""
        if not html:  return "This is an HTML-only email."
        try:
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br)\s*>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            return '\n'.join(chunk for chunk in chunks if chunk)
        except Exception: 
            return "HTML-only email. Please use a compatible client."
    
    def _create_mime_message(self, to_email, subject, html_content, plain_content=None,
                             unsubscribe_url=None, attachments=None, custom_headers=None):
        """Construct the email message object."""
        msg_root = MIMEMultipart('related')
        
        msg_root['Subject'] = Header(subject, 'utf-8').encode()
        msg_root['From'] = formataddr((Header(self.sender_name, 'utf-8').encode(), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid(domain=self.sender_email.split('@')[-1] if '@' in self.sender_email else 'local')
        
        if self.reply_to_email:
            msg_root['Reply-To'] = self.reply_to_email
        
        if unsubscribe_url: 
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')
        
        if custom_headers:
            for key, value in custom_headers.items():
                msg_root.add_header(key, value)
        
        msg_alternative = MIMEMultipart('alternative')
        msg_root.attach(msg_alternative)
        
        if not plain_content:
            plain_content = self._html_to_text(html_content)
            
        msg_alternative.attach(MIMEText(plain_content, 'plain', 'utf-8'))
        msg_alternative.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        if attachments: 
            for filepath in attachments:
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "rb") as f:
                            part = MIMEBase('application', 'octet-stream')
                            part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filepath)}"')
                        msg_root.attach(part)
                    except Exception as e:
                        log.error(f"Could not attach file {filepath}: {e}")
        
        return msg_root

    def test_connection(self):
        """Test SMTP connection credentials by connecting and logging in."""
        if not all([self.smtp_server, self.username, self.password]):
            return False, "SMTP configuration is incomplete."
        
        try:
            context = self._create_secure_ssl_context()
            timeout_val = 45 if PROXY_HOST else 20
            
            if self.use_ssl or self.smtp_port == 465:
                server = ProxySMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=timeout_val)
            else:
                server = ProxySMTP(self.smtp_server, self.smtp_port, timeout=timeout_val)
            
            with server:
                server.ehlo()
                if not self.use_ssl and self.use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(self.username, self.password)
            
            return True, f"Successfully connected to {self.smtp_server}."
        
        except smtplib.SMTPAuthenticationError as e:
            error_msg = e.smtp_error.decode('utf-8', 'ignore') if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            return False, f"Authentication Failed: {error_msg}"
        except Exception as e:
            log.error(f"Connection Test Error: {e}", exc_info=True)
            return False, f"Connection Error: {str(e)}"

    def send_email(self, to_email, subject, html_content, plain_content=None,
                   unsubscribe_url=None, attachments=None, custom_headers=None):
        """
        Send a single email. This is the main method called by Celery tasks.
        It's a self-contained, thread-safe operation.
        """
        try:
            context = self._create_secure_ssl_context()
            timeout_val = 60 if PROXY_HOST else 30
            
            # Instantiate a new SMTP connection object for this specific send operation.
            # This is crucial for thread safety.
            if self.use_ssl or self.smtp_port == 465:
                server = ProxySMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=timeout_val)
            else: 
                server = ProxySMTP(self.smtp_server, self.smtp_port, timeout=timeout_val)
            
            with server:
                server.ehlo()
                if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                    server.starttls(context=context)
                    server.ehlo()
                
                server.login(self.username, self.password)
                
                mime_message = self._create_mime_message(
                    to_email, subject, html_content, plain_content,
                    unsubscribe_url, attachments, custom_headers
                )
                
                server.send_message(mime_message)
            
            with self._lock:
                self._recent_sends.append(datetime.utcnow())
            
            return True, "Sent"
            
        except smtplib.SMTPAuthenticationError as e:
            return False, f"Auth Error: {e}"
        except Exception as e: 
            error_class = self.classify_failure(str(e))
            log.error(f"Send failed to {to_email}: {e} ({error_class})")
            return False, f"{error_class}: {str(e)}"

    def classify_failure(self, error_message):
        """Categorize SMTP errors for better reporting."""
        msg = str(error_message).lower()
        if any(s in msg for s in ["mailbox unavailable", "user unknown", "no such user", "recipient rejected", "invalid address"]):
            return "Hard Bounce"
        elif any(s in msg for s in ["quota", "over quota", "mailbox full"]):
            return "Soft Bounce"
        elif any(s in msg for s in ["rate", "too many", "limit", "throttled"]):
            return "Rate Limited"
        elif any(s in msg for s in ["spam", "blocked", "blacklisted", "content denied"]):
            return "Spam Block"
        elif "authentication" in msg or "credentials" in msg: 
            return "Auth Error"
        elif "socks" in msg or "proxy" in msg: 
            return "Proxy Error"
        elif "timeout" in msg or "connection" in msg:
            return "Connection Error"
        else:
            return "Unknown Error"
