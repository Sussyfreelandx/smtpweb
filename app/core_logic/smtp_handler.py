import smtplib
import ssl
import logging
import re
import os
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

# Try to import PySocks. If not available, set socks to None and proceed using direct sockets.
try:
    import socks  # pip install PySocks
    SOCKS_AVAILABLE = True
except ImportError:
    socks = None
    SOCKS_AVAILABLE = False
except Exception:
    socks = None
    SOCKS_AVAILABLE = False

# Proxy configuration from environment (optional)
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT_ENV = os.environ.get('SMTP_PROXY_PORT')
try:
    PROXY_PORT = int(PROXY_PORT_ENV) if PROXY_PORT_ENV else None
except (ValueError, TypeError):
    PROXY_PORT = None
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')


class ProxySMTP(smtplib.SMTP):
    """
    Custom SMTP class that uses SOCKS5 proxy for the connection
    without patching the global socket module.
    """
    def _get_socket(self, host, port, timeout):
        # If proxy configured and socks library available, use it
        if PROXY_HOST and SOCKS_AVAILABLE:
            log.debug(f"Connecting to {host}:{port} via proxy {PROXY_HOST}:{PROXY_PORT}")
            try:
                return socks.create_connection(
                    (host, port),
                    timeout=timeout,
                    proxy_type=socks.SOCKS5,
                    proxy_addr=PROXY_HOST,
                    proxy_port=PROXY_PORT,
                    proxy_username=PROXY_USER,
                    proxy_password=PROXY_PASS
                )
            except Exception as e:
                log.warning(f"Proxy connection failed ({e}); falling back to direct socket")
                return socket.create_connection((host, port), timeout)
        else:
            return socket.create_connection((host, port), timeout)


class ProxySMTP_SSL(smtplib.SMTP_SSL):
    """
    Custom SMTP_SSL class that uses SOCKS5 proxy for the connection
    without patching the global socket module.
    """
    def _get_socket(self, host, port, timeout):
        if PROXY_HOST and SOCKS_AVAILABLE:
            log.debug(f"Connecting (SSL) to {host}:{port} via proxy {PROXY_HOST}:{PROXY_PORT}")
            try:
                sock = socks.create_connection(
                    (host, port),
                    timeout=timeout,
                    proxy_type=socks.SOCKS5,
                    proxy_addr=PROXY_HOST,
                    proxy_port=PROXY_PORT,
                    proxy_username=PROXY_USER,
                    proxy_password=PROXY_PASS
                )
                new_socket = self.context.wrap_socket(sock, server_hostname=host)
                return new_socket
            except Exception as e:
                log.warning(f"Proxy SSL connection failed ({e}); falling back to direct SSL socket")
                return super()._get_socket(host, port, timeout)
        else:
            return super()._get_socket(host, port, timeout)


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
        
        # Ensure sender_email is never None to prevent 'AttributeError' in make_msgid
        self.sender_email = smtp_config.get('sender_email') or self.username or 'unknown@localhost'
        self.reply_to_email = smtp_config.get('reply_to_email')

        # Connection management
        self._connection = None
        self._max_connection_age = 300
        self._lock = threading.Lock()

        # Rate limiting tracking
        self._recent_sends = deque(maxlen=1000)

    def _create_secure_ssl_context(self):
        """Create a secure SSL context."""
        context = ssl.create_default_context()

        # If proxying, relax hostname checks as tunneling might mismatch SNI
        if PROXY_HOST and SOCKS_AVAILABLE:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            # prefer modern TLS versions
            try:
                context.minimum_version = ssl.TLSVersion.TLSv1_2
            except AttributeError:
                # older Python may not have TLSVersion attribute
                pass

        return context

    def _html_to_text(self, html):
        if not html:
            return "Plain text content not available."
        try:
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br) *>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return text.strip()
        except Exception:
            return "HTML-only email.  Please use a compatible client."

    def _create_mime_message(self, to_email, subject, html_content, plain_content=None,
                             unsubscribe_url=None, attachments=None, custom_headers=None):
        msg_root = MIMEMultipart('related')

        msg_root['Subject'] = Header(subject, 'utf-8').encode()
        msg_root['From'] = formataddr((Header(self.sender_name, 'utf-8').encode(), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        
        # Safe domain extraction
        domain_part = self.sender_email.split('@')[-1] if self.sender_email and '@' in self.sender_email else 'local'
        msg_root['Message-ID'] = make_msgid(domain=domain_part)

        if self.reply_to_email:
            msg_root['Reply-To'] = self.reply_to_email

        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')

        if custom_headers:
            for key, value in custom_headers.items():
                msg_root.add_header(key, str(value))

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
                        log.error(f"Could not attach {filepath}: {e}")

        return msg_root

    def connect(self):
        """Establish connection to SMTP server using Custom Proxy Classes."""
        with self._lock:
            try:
                context = self._create_secure_ssl_context()
                timeout_val = 60 if PROXY_HOST and SOCKS_AVAILABLE else 30

                # Use our custom ProxySMTP classes instead of standard smtplib
                if self.use_ssl or self.smtp_port == 465:
                    self._connection = ProxySMTP_SSL(
                        self.smtp_server, self.smtp_port, context=context, timeout=timeout_val
                    )
                else:
                    self._connection = ProxySMTP(
                        self.smtp_server, self.smtp_port, timeout=timeout_val
                    )

                # Handshake
                try:
                    self._connection.ehlo()
                except smtplib.SMTPHeloError:
                    self._connection.helo()

                # STARTTLS
                if not self.use_ssl and self.use_tls:
                    if hasattr(self._connection, 'has_extn') and self._connection.has_extn('STARTTLS'):
                        self._connection.starttls(context=context)
                        self._connection.ehlo()

                # Login if credentials present
                if self.username and self.password:
                    self._connection.login(self.username, self.password)

                status_msg = f"Connected via Proxy ({PROXY_HOST})" if PROXY_HOST and SOCKS_AVAILABLE else "Connected Direct"
                return True, status_msg

            except smtplib.SMTPAuthenticationError as e:
                error_msg = e.smtp_error.decode() if hasattr(e, 'smtp_error') and isinstance(e.smtp_error, bytes) else str(e)
                return False, f"Auth Failed: {error_msg}"
            except Exception as e:
                log.error(f"Connection Error: {e}")
                return False, f"Connection Error: {str(e)}"

    def disconnect(self):
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

    def test_connection(self):
        """
        Test SMTP connection credentials.
        Required for the 'Test Connection' button in UI.
        """
        if not self.smtp_server or not self.username:
            return False, "SMTP configuration incomplete"

        success, msg = self.connect()
        # Always disconnect after a test
        try:
            self.disconnect()
        except Exception:
            pass
        return success, msg

    def send_email(self, to_email, subject, html_content, plain_content=None,
                   unsubscribe_url=None, attachments=None, custom_headers=None):
        """
        Send a single email.  This is the main method called by the Celery task.
        Wraps send_email_sync for compatibility.
        """
        return self.send_email_sync(
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            plain_content=plain_content,
            unsubscribe_url=unsubscribe_url,
            attachments=attachments,
            custom_headers=custom_headers
        )

    def send_email_sync(self, to_email, subject, html_content, plain_content=None,
                        unsubscribe_url=None, attachments=None, custom_headers=None):
        """
        Send a single email synchronously.
        Uses the scoped ProxySMTP classes to ensure safe proxying.
        """
        try:
            context = self._create_secure_ssl_context()
            timeout_val = 60 if PROXY_HOST and SOCKS_AVAILABLE else 30

            # Instantiate local server object for this send (Thread safe)
            if self.use_ssl or self.smtp_port == 465:
                server = ProxySMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=timeout_val)
            else:
                server = ProxySMTP(self.smtp_server, self.smtp_port, timeout=timeout_val)

            with server:
                try:
                    server.ehlo()
                except Exception:
                    try:
                        server.helo()
                    except Exception:
                        pass

                if not self.use_ssl and self.use_tls and hasattr(server, 'has_extn') and server.has_extn('STARTTLS'):
                    try:
                        server.starttls(context=context)
                        server.ehlo()
                    except Exception:
                        pass

                if self.username and self.password:
                    try:
                        server.login(self.username, self.password)
                    except Exception as e:
                        # Let higher-level logic handle auth errors
                        raise

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
        results = []

        # Lower concurrency if using proxy to prevent congestion
        actual_workers = 2 if PROXY_HOST and SOCKS_AVAILABLE else max_workers

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

        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
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
        cutoff = datetime.utcnow() - timedelta(minutes=60)
        return sum(1 for t in self._recent_sends if t > cutoff)

    def classify_failure(self, error_message):
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
    def __init__(self, profiles):
        self.profiles = profiles
        self.current_index = 0
        self.handlers = {}
        self.failed_profiles = set()
        self._lock = threading.Lock()

    def get_next_handler(self):
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
        return True

    def close_all(self):
        with self._lock:
            for handler in self.handlers.values():
                try:
                    handler.disconnect()
                except Exception:
                    pass
            self.handlers.clear()
