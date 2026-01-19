import smtplib
import ssl
import logging
import re
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header

# Configure logging
log = logging.getLogger(__name__)

class SMTPHandler:
    """
    Handles all SMTP operations using standard smtplib (No AIOSMTP).
    Includes robust multipart message creation and connection logic from Paris Sender Desktop.
    """
    def __init__(self, smtp_config):
        self.smtp_server = smtp_config.get('server')
        try:
            self.smtp_port = int(smtp_config.get('port', 587))
        except (ValueError, TypeError):
            self.smtp_port = 587
            
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username

    def _create_secure_ssl_context(self):
        """Creates a context that allows legacy SSL versions if needed, matching desktop robustness."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _html_to_text(self, html):
        """Converts HTML to plain text for the alternative MIME part."""
        try:
            # Remove scripts and styles
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            # Replace breaks and block elements with newlines
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br)\s*>', '\n', text, flags=re.IGNORECASE)
            # Remove remaining tags
            text = re.sub(r'<[^>]+>', ' ', text)
            # Collapse whitespace
            return re.sub(r'\n\s*\n', '\n\n', text).strip()
        except Exception:
            return "Please enable HTML to view this message."

    def _create_mime_message(self, to_email, subject, html_content, unsubscribe_url=None):
        """Creates a proper Multipart/Related email matching desktop standards."""
        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = Header(subject, 'utf-8').encode()
        msg_root['From'] = formataddr((str(Header(self.sender_name, 'utf-8')), self.sender_email))
        msg_root['To'] = to_email
        msg_root['Date'] = formatdate(localtime=True)
        msg_root['Message-ID'] = make_msgid()
        
        if unsubscribe_url:
            msg_root.add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')
            
        msg_alternative = MIMEMultipart('alternative')
        msg_root.attach(msg_alternative)
        
        # Plain text version
        plain_text = self._html_to_text(html_content)
        msg_alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))
        
        # HTML version
        msg_alternative.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        return msg_root

    def test_connection(self):
        """
        Tests the SMTP connection.
        Logic mirrors the desktop app's threaded tester for maximum compatibility.
        """
        if not self.smtp_server or not self.username:
            return False, "Configuration incomplete."

        server = None
        try:
            context = self._create_secure_ssl_context()
            
            # Connection logic
            if self.use_ssl or self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=15)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15)
            
            # EHLO/HELO
            try:
                server.ehlo()
            except smtplib.SMTPHeloError:
                server.helo()

            # STARTTLS
            if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                server.starttls(context=context)
                server.ehlo()
                
            # Login
            server.login(self.username, self.password)
            server.quit()
            
            return True, "Connection Successful"
            
        except smtplib.SMTPAuthenticationError as e:
            error_msg = str(e.smtp_error) if hasattr(e, 'smtp_error') else str(e)
            return False, f"Authentication Failed: {error_msg}"
        except (socket.timeout, TimeoutError):
            return False, "Connection Timed Out"
        except Exception as e:
            log.error(f"SMTP Test Error: {e}")
            return False, f"Error: {str(e)}"
        finally:
            if server:
                try:
                    server.close()
                except:
                    pass

    def send_email_sync(self, to_email, subject, html_content, unsubscribe_url=None):
        """
        Sends a single email synchronously.
        Used by Celery workers to handle bulk lists without hanging.
        """
        try:
            mime_message = self._create_mime_message(to_email, subject, html_content, unsubscribe_url)
            context = self._create_secure_ssl_context()
            server = None
            
            # Establish connection (fresh connection per batch/email allows for better error recovery in workers)
            if self.use_ssl or self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=20)
            else:
                server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=20)
            
            server.ehlo()
            if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                server.starttls(context=context)
                server.ehlo()
                
            server.login(self.username, self.password)
            server.send_message(mime_message)
            server.quit()
            
            return True, "Sent"
        except Exception as e:
            log.error(f"SMTP Send Failed for {to_email}: {e}")
            return False, str(e)
