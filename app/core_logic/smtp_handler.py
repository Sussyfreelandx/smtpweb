import smtplib
import ssl
import logging
import re
import os
import socket
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
from datetime import datetime, timedelta
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# Try to import socks (PySocks) for proxy support; handle if missing to prevent import errors
try:
    import socks
except ImportError:
    socks = None

log = logging.getLogger(__name__)

PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
# Ensure PROXY_PORT is an integer; default to 1080 if missing or invalid
try:
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT') or 1080)
except (ValueError, TypeError):
    PROXY_PORT = 1080

PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')


class ProxySMTP(smtplib.SMTP):
    """
    Custom SMTP class that supports creating connections via SOCKS5 proxy when PROXY_HOST configured.
    """
    def _get_socket(self, host, port, timeout):
        if PROXY_HOST and socks:
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
            if PROXY_HOST and not socks:
                log.warning("SMTP_PROXY_HOST is set but 'socks' module is not installed. Connecting directly.")
            return socket.create_connection((host, port), timeout)


class ProxySMTP_SSL(smtplib.SMTP_SSL):
    """
    Custom SMTP_SSL class that uses proxy socket if PROXY_HOST set.
    """
    def _get_socket(self, host, port, timeout):
        if PROXY_HOST and socks:
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
            # Wrap with SSL
            new_socket = self.context.wrap_socket(sock, server_hostname=host)
            return new_socket
        else:
            if PROXY_HOST and not socks:
                log.warning("SMTP_PROXY_HOST is set but 'socks' module is not installed. Connecting directly.")
            return super()._get_socket(host, port, timeout)


class SMTPHandler:
    """
    Robust SMTP Handler.
    - Creates connections via ProxySMTP / ProxySMTP_SSL depending on config
    - Provides send_email_sync and send_bulk_threaded
    - Exposes _establish_connection for easier unit testing (can be monkeypatched)
    """

    def __init__(self, smtp_config, pool_size=1):
        """
        smtp_config: dict containing server, port, username, password, use_tls, use_ssl, sender_name, sender_email, reply_to_email
        pool_size: optional number for future pooling (not fully used for persistent pooled sockets here)
        """
        self.smtp_server = smtp_config.get('server')
        self.smtp_port = int(smtp_config.get('port', 587))
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username
        self.reply_to_email = smtp_config.get('reply_to_email')

        # Internal
        self._lock = threading.Lock()
        self._recent_sends = deque(maxlen=1000)
        self.pool_size = max(1, int(pool_size or 1))
        # Basic pool structure (connections are transient, pool maintained for potential reuse)
        self._pool = deque(maxlen=self.pool_size)

    # ---------- Helper functions ----------

    def _create_secure_ssl_context(self):
        """Create a secure SSL context (or relaxed if proxying)."""
        context = ssl.create_default_context()
        if PROXY_HOST:
            # When proxying, certificate verification may be problematic. Use relaxed checks.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            try:
                # Use getattr to avoid AttributeError if TLSVersion is not available in older Python versions
                if hasattr(ssl, 'TLSVersion'):
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                else:
                    # Fallback for older python (deprecated but functional)
                    context.options |= ssl.OP_NO_SSLv2
                    context.options |= ssl.OP_NO_SSLv3
                    context.options |= ssl.OP_NO_TLSv1
                    context.options |= ssl.OP_NO_TLSv1_1
            except Exception:
                pass
        return context

    def _html_to_text(self, html):
        if not html:
            return "Plain text content not available."
        try:
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br) *>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return ' '.join(text.split()).strip()
        except Exception:
            return "HTML-only email."

    def _create_mime_message(self, to_email, subject, html_content, plain_content=None,
                             unsubscribe_url=None, attachments=None, custom_headers=None):
        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = Header(subject, 'utf-8').encode()
        msg_root['From'] = formataddr((Header(self.sender_name or '', 'utf-8').encode(), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid(domain=(self.sender_email.split('@')[-1] if '@' in self.sender_email else 'local'))

        if self.reply_to_email:
            msg_root['Reply-To'] = self.reply_to_email

        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')

        if custom_headers:
            for k, v in custom_headers.items():
                msg_root.add_header(k, v)

        msg_alt = MIMEMultipart('alternative')
        msg_root.attach(msg_alt)

        if not plain_content:
            plain_content = self._html_to_text(html_content) or ''

        msg_alt.attach(MIMEText(plain_content, 'plain', 'utf-8'))
        msg_alt.attach(MIMEText(html_content or '', 'html', 'utf-8'))

        if attachments:
            for filepath in attachments:
                try:
                    if not os.path.exists(filepath):
                        continue
                    with open(filepath, 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filepath)}"')
                    msg_root.attach(part)
                except Exception as e:
                    log.error(f"Could not attach {filepath}: {e}")

        return msg_root

    def _establish_connection(self):
        """
        Establish and return a connected SMTP object.
        Returns (server, True, message) on success, (None, False, message) on failure.
        This method is separated so unit tests can monkeypatch it.
        """
        try:
            context = self._create_secure_ssl_context()
            timeout_val = 60 if PROXY_HOST else 30

            if self.use_ssl or self.smtp_port == 465:
                server = ProxySMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=timeout_val)
            else:
                server = ProxySMTP(self.smtp_server, self.smtp_port, timeout=timeout_val)

            try:
                server.ehlo()
            except Exception:
                server.helo()

            if not self.use_ssl and self.use_tls:
                # Only starttls if extension is supported
                if hasattr(server, 'has_extn') and server.has_extn('STARTTLS'):
                    try:
                        server.starttls(context=context)
                        server.ehlo()
                    except Exception:
                        # proceed without TLS if handshake fails (will likely fail auth)
                        log.debug("STARTTLS handshake failed, continuing without explicit exception raised here.")

            # Login if credentials present
            if self.username:
                server.login(self.username, self.password)

            return server, True, "Connected"
        except smtplib.SMTPAuthenticationError as e:
            err = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e)
            return None, False, f"Auth Failed: {err}"
        except Exception as e:
            log.error(f"Connection Error: {e}")
            return None, False, f"Connection Error: {str(e)}"

    # ---------- Public sending methods ----------

    def connect(self):
        """
        Establish a connection and return (success, message).
        Kept for compatibility with older code that used connect/disconnect pairs.
        """
        server, ok, msg = self._establish_connection()
        if ok and server:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
        return ok, msg

    def disconnect(self):
        """
        No persistent connection is kept by default - this is a placeholder for pooled implementations.
        If pool had persistent connections, close them here.
        """
        # Close any pooled connections
        while self._pool:
            conn = self._pool.pop()
            try:
                conn.quit()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    def test_connection(self):
        """Test connection using _establish_connection then immediately close."""
        if not self.smtp_server or not self.username:
            return False, "SMTP configuration incomplete"
        server, ok, msg = self._establish_connection()
        if ok and server:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
        return ok, msg

    def send_email_sync(self, to_email, subject, html_content, plain_content=None,
                        unsubscribe_url=None, attachments=None, custom_headers=None):
        """
        Send a single email synchronously using a fresh or pooled connection.
        Uses _establish_connection (monkeypatchable for tests).
        """
        try:
            server, ok, msg = self._establish_connection()
            if not ok or not server:
                return False, msg or "Failed to create SMTP connection"

            with server:
                try:
                    server.send_message(self._create_mime_message(
                        to_email, subject, html_content, plain_content,
                        unsubscribe_url, attachments, custom_headers
                    ))
                finally:
                    # context manager will call quit(); ensure closure
                    pass

            # record send
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
        Send multiple emails concurrently using ThreadPoolExecutor.
        email_tasks: list of dicts with keys to_email, subject, html_content, plain_content, attachments, custom_headers
        """
        results = []

        def _send_one(task):
            success, msg = self.send_email_sync(
                task['to_email'],
                task['subject'],
                task['html_content'],
                task.get('plain_content'),
                task.get('unsubscribe_url'),
                task.get('attachments'),
                task.get('custom_headers')
            )
            return {'email': task['to_email'], 'success': success, 'error': None if success else msg}

        with ThreadPoolExecutor(max_workers=min(max_workers, 20)) as ex:
            futures = {ex.submit(_send_one, t): t for t in email_tasks}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    log.error(f"Bulk send worker failed: {e}")
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
    """
    Manage a set of SMTP profiles and provide handlers on demand.
    - profiles: list of dict profile definitions (must include id or username, plus connection details)
    - handler_pool_size: number of handler instances that may be created concurrently (not strict)
    """

    def __init__(self, profiles, handler_pool_size=2):
        self.profiles = profiles or []
        self.current_index = 0
        self.handlers = {}  # profile_key -> SMTPHandler
        self.failed_profiles = set()
        self._lock = threading.Lock()
        self.handler_pool_size = max(1, int(handler_pool_size or 1))

    def get_next_handler(self):
        """
        Return (SMTPHandler instance, profile_key) or (None, reason_str) if none available.
        Selection logic considers priority and sent_today limits (if present).
        """
        with self._lock:
            if not self.profiles:
                return None, "No SMTP profiles available"

            attempts = 0
            total = len(self.profiles)
            while attempts < total:
                profile = self.profiles[self.current_index]
                self.current_index = (self.current_index + 1) % total
                profile_key = profile.get('id') or profile.get('username') or str(attempts)

                if profile_key in self.failed_profiles:
                    attempts += 1
                    continue

                if not self._check_limits(profile):
                    attempts += 1
                    continue

                # Create or reuse handler
                if profile_key not in self.handlers:
                    cfg = {
                        'server': profile.get('server'),
                        'port': profile.get('port'),
                        'username': profile.get('username'),
                        'password': profile.get('password'),
                        'use_tls': profile.get('use_tls', True),
                        'use_ssl': profile.get('use_ssl', False),
                        'sender_name': profile.get('sender_name'),
                        'sender_email': profile.get('sender_email'),
                        'reply_to_email': profile.get('reply_to_email')
                    }
                    try:
                        handler = SMTPHandler(cfg, pool_size=self.handler_pool_size)
                        self.handlers[profile_key] = handler
                    except Exception as e:
                        log.error(f"Failed to create SMTPHandler for profile {profile_key}: {e}")
                        self.failed_profiles.add(profile_key)
                        attempts += 1
                        continue

                return self.handlers[profile_key], profile_key

            return None, "All SMTP profiles exhausted or at limit"

    def _check_limits(self, profile):
        """
        Basic limit check: ensure sent_today < daily_limit if available.
        """
        try:
            daily_limit = int(profile.get('daily_limit', 0) or 0)
            sent_today = int(profile.get('sent_today', 0) or 0)
            if daily_limit and sent_today >= daily_limit:
                return False
        except Exception:
            pass
        return True

    def _mark_failed(self, profile_key):
        """Mark a profile as failed to avoid choosing it for some time."""
        with self._lock:
            self.failed_profiles.add(profile_key)

    def close_all(self):
        """Disconnect and clear all handlers."""
        with self._lock:
            for h in list(self.handlers.values()):
                try:
                    h.disconnect()
                except Exception:
                    pass
            self.handlers.clear()
            self.failed_profiles.clear()
