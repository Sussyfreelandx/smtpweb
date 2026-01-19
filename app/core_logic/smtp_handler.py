import smtplib
import ssl
import logging
import re
import time
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
from datetime import datetime, timedelta
from collections import deque
import os

log = logging.getLogger(__name__)


class SMTPHandler:
    """Handles all SMTP operations with connection pooling and warmup support."""
    
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
        
        self._connection = None
        self._connection_time = None
        self._max_connection_age = 300
        self._lock = threading.Lock()
        
        self.max_retries = 3
        self.retry_delay = 5
        
        self._recent_sends = deque(maxlen=1000)
    
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
        
        msg_root['Subject'] = Header(subject, 'utf-8')
        msg_root['From'] = formataddr((str(Header(self.sender_name, 'utf-8')), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid(domain=self.sender_email.split('@')[-1] if '@' in self.sender_email else 'local')
        msg_root['MIME-Version'] = '1.0'
        
        if self.reply_to_email:
            msg_root['Reply-To'] = self.reply_to_email
        
        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')
        
        if custom_headers: 
            for key, value in custom_headers.items():
                msg_root.add_header(key, value)
        
        if not plain_content:
            plain_content = self._html_to_text(html_content)
        
        part_plain = MIMEText(plain_content, 'plain', 'utf-8')
        part_html = MIMEText(html_content, 'html', 'utf-8')
        
        msg_alt.attach(part_plain)
        msg_alt.attach(part_html)
        
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
        with self._lock:
            try:
                context = self._create_secure_ssl_context()
                
                if self.use_ssl or self.smtp_port == 465:
                    self._connection = smtplib.SMTP_SSL(
                        self.smtp_server,
                        self.smtp_port,
                        context=context,
                        timeout=30
                    )
                else: 
                    self._connection = smtplib.SMTP(
                        self.smtp_server,
                        self.smtp_port,
                        timeout=30
                    )
                
                self._connection.ehlo()
                
                if not self.use_ssl and self.use_tls:
                    if self._connection.has_extn('STARTTLS'):
                        self._connection.starttls(context=context)
                        self._connection.ehlo()
                
                self._connection.login(self.username, self.password)
                self._connection_time = datetime.utcnow()
                
                return True, "Connected successfully"
            
            except smtplib.SMTPAuthenticationError as e:
                error_msg = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                log.error(f"SMTP Authentication Error: {error_msg}")
                return False, f"Authentication Failed: {error_msg}"
            
            except smtplib.SMTPConnectError as e:
                log.error(f"SMTP Connection Error: {e}")
                return False, f"Connection Error: {str(e)}"
            
            except Exception as e:
                log.error(f"SMTP Error: {e}")
                return False, str(e)
    
    def disconnect(self):
        """Close SMTP connection."""
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
    
    def _ensure_connection(self):
        """Ensure we have a valid connection."""
        if self._connection is None:
            return self.connect()
        
        if self._connection_time:
            age = (datetime.utcnow() - self._connection_time).total_seconds()
            if age > self._max_connection_age:
                self.disconnect()
                return self.connect()
        
        try:
            self._connection.noop()
            return True, "Connection valid"
        except Exception: 
            self.disconnect()
            return self.connect()
    
    def test_connection(self):
        """Test SMTP connection without sending."""
        if not self.smtp_server or not self.username: 
            return False, "SMTP configuration incomplete"
        
        if not self.password:
            return False, "Password not configured"
        
        server = None
        try:
            context = self._create_secure_ssl_context()
            
            if self.use_ssl or self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=15)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15)
            
            server.ehlo()
            
            if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                server.starttls(context=context)
                server.ehlo()
            
            server.login(self.username, self.password)
            server.quit()
            
            return True, "Connection successful!"
        
        except smtplib.SMTPAuthenticationError as e:
            error_msg = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            return False, f"Authentication Failed: {error_msg}"
        
        except smtplib.SMTPConnectError as e:
            return False, f"Connection Error: {str(e)}"
        
        except smtplib.SMTPServerDisconnected as e:
            return False, f"Server Disconnected: {str(e)}"
        
        except Exception as e:
            log.error(f"SMTP Test Failed: {e}")
            return False, str(e)
        
        finally:
            if server:
                try:
                    server.close()
                except Exception:
                    pass
    
    def send_email_sync(self, to_email, subject, html_content, plain_content=None,
                        unsubscribe_url=None, attachments=None, custom_headers=None):
        """Send a single email synchronously with retry logic."""
        last_error = None
        
        for attempt in range(self.max_retries):
            try:
                mime_message = self._create_mime_message(
                    to_email,
                    subject,
                    html_content,
                    plain_content=plain_content,
                    unsubscribe_url=unsubscribe_url,
                    attachments=attachments,
                    custom_headers=custom_headers
                )
                
                context = self._create_secure_ssl_context()
                
                if self.use_ssl or self.smtp_port == 465:
                    server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30)
                else:
                    server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
                
                with server:
                    server.ehlo()
                    
                    if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                        server.starttls(context=context)
                        server.ehlo()
                    
                    server.login(self.username, self.password)
                    server.send_message(mime_message)
                
                self._recent_sends.append(datetime.utcnow())
                return True, "Sent"
            
            except smtplib.SMTPAuthenticationError as e: 
                error_msg = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                log.error(f"SMTP Auth Error for {to_email}: {error_msg}")
                return False, f"Authentication Error: {error_msg}"
            
            except smtplib.SMTPRecipientsRefused as e: 
                log.error(f"SMTP Recipients Refused for {to_email}: {e}")
                return False, f"Recipient Refused: {to_email}"
            
            except smtplib.SMTPSenderRefused as e:
                log.error(f"SMTP Sender Refused: {e}")
                return False, "Sender Refused"
            
            except smtplib.SMTPDataError as e:
                error_msg = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                log.error(f"SMTP Data Error for {to_email}: {error_msg}")
                return False, f"Data Error: {error_msg}"
            
            except smtplib.SMTPServerDisconnected as e:
                log.warning(f"SMTP Server Disconnected (attempt {attempt + 1}): {e}")
                last_error = f"Server Disconnected: {str(e)}"
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
            
            except Exception as e: 
                log.error(f"SMTP sending failed for {to_email}: {e}")
                last_error = str(e)
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
        
        return False, last_error or "Unknown error after retries"
    
    def send_with_connection_pool(self, to_email, subject, html_content, **kwargs):
        """Send using persistent connection."""
        with self._lock:
            connected, error = self._ensure_connection()
            
            if not connected:
                return False, error
            
            try:
                mime_message = self._create_mime_message(
                    to_email,
                    subject,
                    html_content,
                    **kwargs
                )
                
                self._connection.send_message(mime_message)
                self._recent_sends.append(datetime.utcnow())
                return True, "Sent"
            
            except smtplib.SMTPServerDisconnected: 
                self._connection = None
                connected, error = self.connect()
                
                if not connected:
                    return False, error
                
                try:
                    self._connection.send_message(mime_message)
                    self._recent_sends.append(datetime.utcnow())
                    return True, "Sent"
                except Exception as e:
                    return False, str(e)
            
            except Exception as e:
                log.error(f"Send error for {to_email}: {e}")
                return False, str(e)
    
    def get_recent_send_count(self, minutes=60):
        """Get count of emails sent in the last N minutes."""
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        return sum(1 for t in self._recent_sends if t > cutoff)
    
    def get_sends_per_hour(self):
        """Get current sends per hour rate."""
        return self.get_recent_send_count(60)
    
    def classify_failure(self, error_message):
        """Classify the type of failure from error message."""
        error_lower = error_message.lower()
        
        if any(s in error_lower for s in ['user unknown', 'no such user', 'mailbox not found', 
                                          'recipient rejected', 'invalid recipient']):
            return 'hard_bounce'
        
        if any(s in error_lower for s in ['mailbox full', 'over quota', 'quota exceeded']):
            return 'soft_bounce_quota'
        
        if any(s in error_lower for s in ['temporarily', 'try again', 'deferred']):
            return 'soft_bounce_temp'
        
        if any(s in error_lower for s in ['blocked', 'blacklisted', 'spam']):
            return 'blocked'
        
        if any(s in error_lower for s in ['rate', 'limit', 'throttle']):
            return 'rate_limited'
        
        if 'authentication' in error_lower:
            return 'auth_error'
        
        return 'unknown'


class SMTPRotationManager:
    """Manages rotation between multiple SMTP profiles."""
    
    def __init__(self, profiles):
        self.profiles = profiles
        self.current_index = 0
        self.handlers = {}
        self.failed_profiles = set()
        self._lock = threading.Lock()
    
    def get_next_handler(self):
        """Get the next available SMTP handler."""
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
        """Check if profile is within limits."""
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
        """Mark a profile as failed."""
        with self._lock:
            self.failed_profiles.add(profile_id)
    
    def reset_failed_profiles(self):
        """Reset the failed profiles set."""
        with self._lock:
            self.failed_profiles.clear()
    
    def close_all(self):
        """Close all SMTP connections."""
        with self._lock:
            for handler in self.handlers.values():
                try:
                    handler.disconnect()
                except Exception:
                    pass
            self.handlers.clear()


class WarmupManager:
    """Manages warmup schedule for new SMTP profiles."""
    
    DEFAULT_SCHEDULE = [
        {'day': 1, 'limit': 20},
        {'day': 2, 'limit': 40},
        {'day': 3, 'limit': 75},
        {'day': 4, 'limit': 125},
        {'day': 5, 'limit': 200},
        {'day': 6, 'limit': 300},
        {'day': 7, 'limit': 400},
        {'day': 14, 'limit': 600},
        {'day': 21, 'limit': 800},
        {'day': 30, 'limit': 1000},
    ]
    
    def __init__(self, custom_schedule=None):
        self.schedule = custom_schedule or self.DEFAULT_SCHEDULE
    
    def get_daily_limit(self, start_date, current_date=None):
        """Get the daily sending limit based on warmup day."""
        if current_date is None:
            current_date = datetime.utcnow().date()
        
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        
        days_since_start = (current_date - start_date).days + 1
        
        limit = self.schedule[0]['limit']
        
        for tier in self.schedule:
            if days_since_start >= tier['day']:
                limit = tier['limit']
            else:
                break
        
        return limit
    
    def is_warmup_complete(self, start_date, current_date=None):
        """Check if warmup period is complete."""
        if current_date is None:
            current_date = datetime.utcnow().date()
        
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        
        days_since_start = (current_date - start_date).days + 1
        
        return days_since_start > self.schedule[-1]['day']
    
    def get_warmup_progress(self, start_date, current_date=None):
        """Get warmup progress percentage."""
        if current_date is None:
            current_date = datetime.utcnow().date()
        
        if isinstance(start_date, datetime):
            start_date = start_date.date()
        
        days_since_start = (current_date - start_date).days + 1
        total_days = self.schedule[-1]['day']
        
        progress = min(100, round(days_since_start / total_days * 100, 1))
        
        return {
            'day': days_since_start,
            'total_days': total_days,
            'progress_percent': progress,
            'current_limit': self.get_daily_limit(start_date, current_date),
            'is_complete': self.is_warmup_complete(start_date, current_date)
        }
