import smtplib
import ssl
import logging
import re
import os
import socket
import threading
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
from datetime import datetime, timedelta
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# Handle optional PySocks dependency to prevent import errors
try:
    import socks
except ImportError:
    socks = None

log = logging.getLogger(__name__)

PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')

# Fix logic to ensure PROXY_PORT is an integer if set, or defaults to 1080
_proxy_port_env = os.environ.get('SMTP_PROXY_PORT')
PROXY_PORT = int(_proxy_port_env) if _proxy_port_env and _proxy_port_env.isdigit() else 1080

PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')


class ProxySMTP(smtplib.SMTP):
    """
    Custom SMTP class that uses SOCKS5 proxy for the connection
    without patching the global socket module.
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
                log.warning("Proxy configured but PySocks not installed. Connecting directly.")
            return socket.create_connection((host, port), timeout)


class ProxySMTP_SSL(smtplib.SMTP_SSL):
    """
    Custom SMTP_SSL class that uses SOCKS5 proxy for the connection
    without patching the global socket module.
    """
    def _get_socket(self, host, port, timeout):
        if PROXY_HOST and socks:
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
            # 2. Wrap the socket with SSL
            new_socket = self.context.wrap_socket(sock, server_hostname=host)
            return new_socket
        else:
            if PROXY_HOST and not socks:
                log.warning("Proxy configured but PySocks not installed. Connecting directly.")
            return super()._get_socket(host, port, timeout)


class SMTPHandler:
    """
    Robust SMTP Handler integrated from Paris Sender Desktop logic.
    Supports connection pooling, multi-threaded bulk sending, warmup, and SOCKS5 Proxying.
    """

    def __init__(self, smtp_config, pool_size=3):
        self.smtp_server = smtp_config.get('server')
        self.smtp_port = int(smtp_config.get('port', 587))
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username
        self.reply_to_email = smtp_config.get('reply_to_email')

        # Connection pooling
        self._pool_size = max(1, int(pool_size))
        self._pool = deque()
        self._pool_lock = threading.Lock()
        self._active_connections = 0
        self._max_connection_age = 300  # seconds
        self._connection_last_used = {}  # map conn -> last_used timestamp

        # Global lock for operations that require it
        self._lock = threading.Lock()

        # Rate limiting tracking
        self._recent_sends = deque(maxlen=1000)

    def _create_secure_ssl_context(self):
        """Create a secure SSL context."""
        context = ssl.create_default_context()

        # If proxying, relax hostname checks as tunneling might mismatch SNI
        if PROXY_HOST and socks:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            # Prefer TLSv1.2+
            try:
                # Check for attribute existence for compatibility
                if hasattr(ssl, 'TLSVersion'):
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
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
                        log.error(f"Could not attach {filepath}: {e}")

        return msg_root

    # ------------------ Connection Pooling ------------------

    def _establish_connection(self):
        """
        Establish a new SMTP connection, return the connection object.
        """
        context = self._create_secure_ssl_context()
        timeout_val = 60 if PROXY_HOST else 30

        # Choose ProxySMTP classes where appropriate
        try:
            if self.use_ssl or self.smtp_port == 465:
                conn = ProxySMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=timeout_val)
            else:
                conn = ProxySMTP(self.smtp_server, self.smtp_port, timeout=timeout_val)

            # Handshake
            try:
                conn.ehlo()
            except smtplib.SMTPHeloError:
                conn.helo()

            # STARTTLS
            if not self.use_ssl and self.use_tls:
                if conn.has_extn('STARTTLS'):
                    conn.starttls(context=context)
                    conn.ehlo()

            # Login if credentials provided
            if self.username and self.password:
                conn.login(self.username, self.password)

            # record usage
            with self._pool_lock:
                self._active_connections += 1
                self._connection_last_used[conn] = time.time()

            return conn, True, "Connected"
        except smtplib.SMTPAuthenticationError as e:
            err = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            log.error(f"SMTP auth error: {err}")
            return None, False, f"Auth Failed: {err}"
        except Exception as e:
            log.error(f"Connection error to {self.smtp_server}:{self.smtp_port} - {e}")
            return None, False, f"Connection Error: {str(e)}"

    def _acquire_connection(self, timeout=15):
        """
        Acquire a connection from the pool (or create a new one if pool not at max).
        Waits up to 'timeout' seconds for a free connection.
        """
        deadline = time.time() + timeout
        while True:
            with self._pool_lock:
                # Flush stale connections
                for conn in list(self._pool):
                    last_used = self._connection_last_used.get(conn, 0)
                    if time.time() - last_used > self._max_connection_age:
                        try:
                            conn.quit()
                        except:
                            try:
                                conn.close()
                            except:
                                pass
                        self._pool.remove(conn)
                        self._active_connections = max(0, self._active_connections - 1)
                        self._connection_last_used.pop(conn, None)

                if self._pool:
                    conn = self._pool.popleft()
                    self._connection_last_used[conn] = time.time()
                    return conn

                # If we can create new connection
                if self._active_connections < self._pool_size:
                    conn, ok, msg = self._establish_connection()
                    if conn:
                        return conn
                    else:
                        # Connection failed; log and possibly retry
                        log.error(f"Failed to establish connection: {msg}")
                        # If can't create connection, fall back to waiting for existing one
                # else: pool at max, wait
            if time.time() > deadline:
                raise TimeoutError("Timeout acquiring SMTP connection from pool")
            time.sleep(0.1)

    def _release_connection(self, conn):
        """
        Release a connection back into the pool. Validate it's open, otherwise close and decrement active count.
        """
        if not conn:
            return
        with self._pool_lock:
            try:
                # Simple noop check by sending noop if supported
                if hasattr(conn, 'noop'):
                    try:
                        conn.noop()
                    except:
                        try:
                            conn.quit()
                        except:
                            try:
                                conn.close()
                            except:
                                pass
                        self._active_connections = max(0, self._active_connections - 1)
                        self._connection_last_used.pop(conn, None)
                        return
                # return to pool
                self._connection_last_used[conn] = time.time()
                self._pool.append(conn)
            except Exception:
                try:
                    conn.quit()
                except:
                    try:
                        conn.close()
                    except:
                        pass
                self._active_connections = max(0, self._active_connections - 1)
                self._connection_last_used.pop(conn, None)

    def connect(self):
        """
        Establish a single connection and store it in pool (for testing/one-off).
        """
        try:
            conn, ok, msg = self._establish_connection()
            if conn:
                # store in pool for reuse
                with self._pool_lock:
                    self._pool.append(conn)
                return True, msg
            return False, msg
        except Exception as e:
            return False, str(e)

    def disconnect(self):
        """
        Close all pooled connections and clear pool.
        """
        with self._pool_lock:
            while self._pool:
                conn = self._pool.popleft()
                try:
                    conn.quit()
                except:
                    try:
                        conn.close()
                    except:
                        pass
                self._connection_last_used.pop(conn, None)
            self._active_connections = 0

    def test_connection(self):
        """
        Test SMTP connection credentials by creating a connection and closing it.
        """
        if not self.smtp_server or not self.username:
            return False, "SMTP configuration incomplete"

        success, msg = self.connect()
        # Immediately disconnect the created connections
        self.disconnect()
        return success, msg

    # ------------------ Sending Methods ------------------

    def send_email(self, to_email, subject, html_content, plain_content=None,
                   unsubscribe_url=None, attachments=None, custom_headers=None):
        """
        Backwards-compatible wrapper that calls send_email_sync.
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
        Send a single email synchronously using a pooled connection.
        """
        try:
            mime_message = self._create_mime_message(
                to_email, subject, html_content, plain_content,
                unsubscribe_url, attachments, custom_headers
            )

            conn = self._acquire_connection(timeout=10)
            try:
                # Ensure HELO/EHLO and STARTTLS if needed (connection may be reused and already in good state)
                try:
                    conn.send_message(mime_message)
                except smtplib.SMTPServerDisconnected:
                    # Re-establish this connection and retry once
                    try:
                        conn, ok, msg = self._establish_connection()
                        if not conn:
                            return False, f"Connection re-establish failed: {msg}"
                        conn.send_message(mime_message)
                    except Exception as e:
                        return False, f"Send failed after reconnect: {str(e)}"
            finally:
                # Update last used and release back
                try:
                    self._release_connection(conn)
                except Exception:
                    pass

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
        Threaded bulk sending that uses pooled send_email_sync calls for concurrency.
        email_tasks: list of dicts with keys:
            'to_email', 'subject', 'html_content', optional 'plain_content', 'attachments', 'custom_headers'
        """
        results = []

        actual_workers = min(max_workers, len(email_tasks) or 1)

        def _send_single_task(task):
            try:
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
            except Exception as e:
                log.error(f"Thread worker send error: {e}")
                return {'email': task.get('to_email', 'unknown'), 'success': False, 'error': str(e)}

        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            future_to_task = {executor.submit(_send_single_task, task): task for task in email_tasks}
            for future in as_completed(future_to_task):
                try:
                    res = future.result()
                    results.append(res)
                except Exception as e:
                    log.error(f"Bulk thread future exception: {e}")
                    results.append({'email': 'unknown', 'success': False, 'error': str(e)})

        return results

    # ------------------ Utilities ------------------

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
    Simple rotation manager that maintains a list of SMTP profiles and returns the next handler.
    """
    def __init__(self, profiles, handler_pool_size=3):
        """
        profiles: list of profile dicts (as returned by SMTPServer.to_dict() plus relevant metadata)
        handler_pool_size: default pool size for each handler
        """
        self.profiles = profiles or []
        self.current_index = 0
        self.handlers = {}
        self.failed_profiles = set()
        self._lock = threading.Lock()
        self.handler_pool_size = handler_pool_size

    def get_next_handler(self):
        """
        Return (handler_instance, profile_id_or_message) or (None, error_message).
        Will skip profiles that appear to be exhausted and mark them failed if connection/auth issues occur.
        """
        with self._lock:
            if not self.profiles:
                return None, "No SMTP profiles available"

            attempts = 0
            total = len(self.profiles)
            while attempts < total:
                profile = self.profiles[self.current_index]
                self.current_index = (self.current_index + 1) % total

                # profile may be an ORM object or dict, normalize to dict
                profile_id = profile.get('id') if isinstance(profile, dict) else getattr(profile, 'id', None)
                key = profile_id or (profile.get('username') if isinstance(profile, dict) else getattr(profile, 'username', None))

                if key in self.failed_profiles:
                    attempts += 1
                    continue

                # Check simple usage limits if present
                if isinstance(profile, dict):
                    daily_limit = profile.get('daily_limit', 500)
                    sent_today = profile.get('sent_today', 0)
                else:
                    daily_limit = getattr(profile, 'daily_limit', 500)
                    sent_today = getattr(profile, 'sent_today', 0)

                if sent_today >= daily_limit:
                    attempts += 1
                    continue

                # Return existing or new handler
                if key not in self.handlers:
                    try:
                        # Safely copy dict to avoid modifying original
                        if isinstance(profile, dict):
                            cfg = profile.copy()
                        else:
                            cfg = profile.to_dict()
                        
                        cfg['username'] = profile.get('username') if isinstance(profile, dict) else getattr(profile, 'username', None)
                        # keep port and boolean fields
                        cfg['port'] = profile.get('port') if isinstance(profile, dict) else getattr(profile, 'port', 587)
                        cfg['use_tls'] = profile.get('use_tls', True) if isinstance(profile, dict) else getattr(profile, 'use_tls', True)
                        cfg['use_ssl'] = profile.get('use_ssl', False) if isinstance(profile, dict) else getattr(profile, 'use_ssl', False)
                        cfg['sender_name'] = profile.get('sender_name') if isinstance(profile, dict) else getattr(profile, 'sender_name', '')
                        cfg['sender_email'] = profile.get('sender_email') if isinstance(profile, dict) else getattr(profile, 'sender_email', None)
                        cfg['password'] = profile.get('password') if isinstance(profile, dict) else (profile.get_password() if hasattr(profile, 'get_password') else None)

                        handler = SMTPHandler(cfg, pool_size=self.handler_pool_size)
                        # Try a test connection lazily when first used
                        self.handlers[key] = {
                            'handler': handler,
                            'profile': profile
                        }
                    except Exception as e:
                        log.error(f"Failed to create handler for profile {key}: {e}")
                        self.failed_profiles.add(key)
                        attempts += 1
                        continue

                return self.handlers[key]['handler'], key

            return None, "All SMTP profiles exhausted or at limit"

    def _mark_failed(self, key):
        with self._lock:
            self.failed_profiles.add(key)

    def close_all(self):
        with self._lock:
            for entry in list(self.handlers.values()):
                handler = entry.get('handler')
                try:
                    handler.disconnect()
                except:
                    pass
            self.handlers.clear()
            self.failed_profiles.clear()
