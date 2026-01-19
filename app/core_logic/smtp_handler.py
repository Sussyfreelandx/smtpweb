import smtplib
import ssl
import logging
import re
import time
import threading
import os
import socks  # Requires: pip install PySocks
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

log = logging.getLogger(__name__)

# --- GLOBAL PROXY CONFIGURATION ---
# Checks for Render Environment Variables
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

# If proxy vars are set, patch smtplib to route through the VPS
if PROXY_HOST:
    log.info(f"🔌 SMTP Proxy Active: Tunneling via {PROXY_HOST}:{PROXY_PORT}")
    if PROXY_USER and PROXY_PASS:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
    
    # This line forces all SMTP traffic through the proxy
    socks.wrap_module(smtplib)

class SMTPHandler:
    """
    Robust SMTP Handler integrated from Paris Sender Desktop logic.
    Supports connection pooling, multi-threaded bulk sending, warmup, and SOCKS5 Proxying.
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
        
        # Connection management
        self._connection = None
        self._connection_time = None
        self._max_connection_age = 300  # Refresh connection every 5 minutes
        self._lock = threading.Lock()
        
        # Rate limiting tracking
        self._recent_sends = deque(maxlen=1000)
        
        # Configuration
        self.max_retries = 3
        self.retry_delay = 5
    
    def _create_secure_ssl_context(self):
        """Create a secure SSL context."""
        context = ssl.create_default_context()
        
        # If proxying, we must relax hostname checks because the tunnel resolves IPs differently
        if PROXY_HOST:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            
        return context
    
    def _html_to_text(self, html):
        """Convert HTML to plain text using regex (Robust method from desktop app)."""
        if not html: return "Plain text content not available."
        try:
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br) *>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return text.strip()
        except Exception:
            return "HTML-only email. Please use a compatible client."
    
    def _create_mime_message(self, to_email, subject, html_content, plain_content=None,
                             unsubscribe_url=None, attachments=None, custom_headers=None):
        """Create a MIME message with robust attachment and header support."""
        msg_root = MIMEMultipart('related')
        
        # Proper UTF-8 Header Encoding
        msg_root['Subject'] = Header(subject, 'utf-8').encode()
        msg_root['From'] = formataddr((Header(self.sender_name, 'utf-8').encode(), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid(domain=self.sender_email.split('@')[-1] if '@' in self.sender_email else 'local')
        
        if self.reply_to_email:
            msg_root['Reply-To'] = self.reply_to_email
        
        # Headers for Unsubscribe and Tracking
        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')
        
        if custom_headers:
            for key, value in custom_headers.items():
                msg_root.add_header(key, value)
        
        msg_alternative = MIMEMultipart('alternative')
        msg_root.attach(msg_alternative)
        
        # Content Parts
        if not plain_content:
            plain_content = self._html_to_text(html_content)
            
        msg_alternative.attach(MIMEText(plain_content, 'plain', 'utf-8'))
        msg_alternative.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        # Attachments
        if attachments:
            for filepath in attachments:
                if os.path.exists(filepath):
                    try:
                        with open(filepath, "rb") as f:
                            part = MIMEBase('application', 'octet-stream')
                            part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header(
                            'Content-Disposition',
                            f'attachment; filename="{os.path.basename(filepath)}"'
                        )
                        msg_root.attach(part)
                    except Exception as e:
                        log.error(f"⚠️ Could not attach {filepath}: {e}")
        
        return msg_root

    def connect(self):
        """Establish connection to SMTP server (Proxied automatically via socks.wrap_module)."""
        with self._lock:
            try:
                context = self._create_secure_ssl_context()
                
                # Force shorter timeouts for cloud environments to fail fast if proxy is stuck
                timeout_val = 30
                
                if self.use_ssl or self.smtp_port == 465:
                    self._connection = smtplib.SMTP_SSL(
                        self.smtp_server, self.smtp_port, context=context, timeout=timeout_val
                    )
                else: 
                    self._connection = smtplib.SMTP(
                        self.smtp_server, self.smtp_port, timeout=timeout_val
                    )
                
                # Handshake
                try:
                    self._connection.ehlo()
                except smtplib.SMTPHeloError:
                    self._connection.helo()
                
                # STARTTLS
                if not self.use_ssl and self.use_tls:
                    if self._connection.has_extn('STARTTLS'):
                        self._connection.starttls(context=context)
                        self._connection.ehlo()
                
                self._connection.login(self.username, self.password)
                self._connection_time = datetime.utcnow()
                
                status_msg = "Connected via Proxy" if PROXY_HOST else "Connected Direct"
                return True, status_msg
            
            except smtplib.SMTPAuthenticationError as e:
                error_msg = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                return False, f"Auth Failed: {error_msg}"
            except Exception as e:
                log.error(f"Connection Error: {e}")
                return False, f"Connection Error: {str(e)}"

    def disconnect(self):
        """Safely close SMTP connection."""
        with self._lock:
            if self._connection:
                try:
                    self._connection.quit()
                except Exception:
                    try:
                        self._connection.close()
                    except Exception:
                        pass
                finally:
                    self._connection = None
                    self._connection_time = None

    def test_connection(self):
        """Test SMTP connection credentials."""
        if not self.smtp_server or not self.username:
            return False, "SMTP configuration incomplete"
        
        success, msg = self.connect()
        self.disconnect() # Always disconnect after a test
        return success, msg

    def send_email_sync(self, to_email, subject, html_content, plain_content=None,
                        unsubscribe_url=None, attachments=None, custom_headers=None):
        """
        Send a single email synchronously. 
        Re-establishes connection if needed.
        """
        # Always create a fresh connection for threaded tasks when proxying to prevent socket pipe errors
        server = None
        try:
            context = self._create_secure_ssl_context()
            timeout_val = 30
            
            if self.use_ssl or self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=timeout_val)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=timeout_val)
            
            with server:
                try: server.ehlo()
                except: server.helo()
                
                if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                    server.starttls(context=context)
                    server.ehlo()
                
                server.login(self.username, self.password)
                
                mime_message = self._create_mime_message(
                    to_email, subject, html_content, plain_content,
                    unsubscribe_url, attachments, custom_headers
                )
                
                server.send_message(mime_message)
            
            self._recent_sends.append(datetime.utcnow())
            return True, "Sent"
            
        except smtplib.SMTPAuthenticationError as e:
            return False, f"Auth Error: {e}"
        except Exception as e:
            error_class = self.classify_failure(str(e))
            log.error(f"Send failed to {to_email}: {e} ({error_class})")
            return False, f"{error_class}: {str(e)}"

    def send_bulk_threaded(self, email_tasks, max_workers=5):
        """
        Send a batch of emails using multiple threads.
        When PROXY_HOST is set, all threads automatically use the proxy tunnel.
        """
        results = []
        
        def _send_single_task(task):
            success, msg = self.send_email_sync(
                task['to_email'],
                task['subject'],
                task['html_content'],
                task.get('plain_content'),
                task.get('unsubscribe_url'),
                task.get('attachments'),
                task.get('custom_headers')
            )
            return {
                'email': task['to_email'],
                'success': success,
                'error': msg if not success else None
            }

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_email = {executor.submit(_send_single_task, task): task for task in email_tasks}
            
            for future in as_completed(future_to_email):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    log.error(f"Thread worker failed: {e}")
                    results.append({'email': 'unknown', 'success': False, 'error': str(e)})
                    
        return results

    def get_sends_per_hour(self):
        """Get count of emails sent in the last 60 minutes."""
        cutoff = datetime.utcnow() - timedelta(minutes=60)
        return sum(1 for t in self._recent_sends if t > cutoff)

    def classify_failure(self, error_message):
        """Classify the failure type based on the exception message."""
        msg = str(error_message).lower()
        
        if any(s in msg for s in ["mailbox unavailable", "user unknown", "no such user", "recipient rejected", "invalid address"]):
            return "hard_bounce"
        elif any(s in msg for s in ["quota", "over quota", "mailbox full"]):
            return "soft_bounce"
        elif any(s in msg for s in ["rate", "too many", "limit", "throttled"]):
            return "rate_limited"
        elif any(s in msg for s in ["spam", "blocked", "blacklisted", "content denied"]):
            return "spam_block"
        elif "authentication" in msg or "credentials" in msg:
            return "auth_error"
        elif "socks" in msg or "proxy" in msg:
            return "proxy_error"
        elif "timeout" in msg or "connection" in msg:
            return "connection_error"
        else:
            return "unknown_error"


class SMTPRotationManager:
    """Manages rotation between multiple SMTP profiles."""
    
    def __init__(self, profiles):
        self.profiles = profiles
        self.current_index = 0
        self.handlers = {}
        self.failed_profiles = set()
        self._lock = threading.Lock()
    
    def get_next_handler(self):
        """Get the next available SMTP handler from the pool."""
        with self._lock:
            if not self.profiles:
                return None, "No SMTP profiles available"
            
            attempts = 0
            while attempts < len(self.profiles):
                profile = self.profiles[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.profiles)
                
                profile_id = profile.get('id') or profile.get('username')
                
                if profile_id in self.failed_profiles:
                    attempts += 1
                    continue
                
                if self._check_limits(profile):
                    if profile_id not in self.handlers:
                        self.handlers[profile_id] = SMTPHandler(profile)
                    
                    return self.handlers[profile_id], None
                
                attempts += 1
            
            return None, "All SMTP profiles exhausted or at limit"
    
    def _check_limits(self, profile):
        daily_limit = profile.get('daily_limit', 500)
        sent_today = profile.get('sent_today', 0)
        
        if sent_today >= daily_limit: 
            return False
        
        hourly_limit = profile.get('hourly_limit', 100)
        if 'handler' in profile:
            sent_hour = profile['handler'].get_sends_per_hour()
            if sent_hour >= hourly_limit:
                return False
        
        return True
    
    def mark_profile_failed(self, profile_id):
        with self._lock:
            self.failed_profiles.add(profile_id)
            log.warning(f"SMTP Profile {profile_id} marked as failed.")
    
    def reset_failed_profiles(self):
        with self._lock:
            self.failed_profiles.clear()
            
    def close_all(self):
        with self._lock:
            for handler in self.handlers.values():
                try: handler.disconnect()
                except: pass
            self.handlers.clear()


class WarmupManager:
    """Manages intelligent warmup schedules for new SMTP profiles."""
    
    DEFAULT_SCHEDULE = [
        {'day': 1, 'limit': 20},
        {'day': 2, 'limit': 40},
        {'day': 3, 'limit': 80},
        {'day': 4, 'limit': 150},
        {'day': 5, 'limit': 300},
        {'day': 6, 'limit': 500},
        {'day': 7, 'limit': 800},
        {'day': 14, 'limit': 1500},
        {'day': 30, 'limit': 5000},
    ]
    
    def __init__(self, custom_schedule=None):
        self.schedule = custom_schedule or self.DEFAULT_SCHEDULE
    
    def get_daily_limit(self, start_date, current_date=None):
        if current_date is None:
            current_date = datetime.utcnow().date()
        
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        
        if not start_date:
            return self.schedule[0]['limit']

        days_since_start = (current_date - start_date).days + 1
        limit = self.schedule[-1]['limit']
        
        for tier in self.schedule:
            if days_since_start <= tier['day']:
                limit = tier['limit']
                break
        
        return limit
