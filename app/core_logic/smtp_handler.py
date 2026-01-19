import smtplib
import ssl
import logging
import re
import socket
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header

log = logging.getLogger(__name__)

class SMTPHandler:
    def __init__(self, smtp_config):
        self.smtp_server = smtp_config.get('server')
        try:
            self.smtp_port = int(smtp_config.get('port', 587))
        except:
            self.smtp_port = 587
        self.username = smtp_config.get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username
        self.server_conn = None

    def _create_secure_ssl_context(self):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _html_to_text(self, html):
        # ... (Keep existing _html_to_text function) ...
        try:
            text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'</(p|h[1-6]|li|div|tr|br)\s*>', '\n', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return re.sub(r'\n\s*\n', '\n\n', text).strip()
        except:
            return "View as HTML."

    def _create_mime_message(self, to_email, subject, html_content, unsubscribe_url=None):
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
        msg_alternative.attach(MIMEText(self._html_to_text(html_content), 'plain', 'utf-8'))
        msg_alternative.attach(MIMEText(html_content, 'html', 'utf-8'))
        return msg_root

    def connect(self):
        """Establishes a persistent connection."""
        try:
            context = self._create_secure_ssl_context()
            if self.use_ssl or self.smtp_port == 465:
                self.server_conn = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30)
            else:
                self.server_conn = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30)
            
            self.server_conn.ehlo()
            if not self.use_ssl and self.use_tls and self.server_conn.has_extn('STARTTLS'):
                self.server_conn.starttls(context=context)
                self.server_conn.ehlo()
                
            self.server_conn.login(self.username, self.password)
            return True, "Connected"
        except Exception as e:
            self.quit()
            return False, str(e)

    def send_message_existing_conn(self, to_email, subject, html_content, unsubscribe_url=None):
        """Sends using the already open connection."""
        if not self.server_conn:
            return False, "No active connection"
            
        try:
            msg = self._create_mime_message(to_email, subject, html_content, unsubscribe_url)
            self.server_conn.send_message(msg)
            return True, "Sent"
        except Exception as e:
            # If pipe broken, try to reconnect once
            log.warning(f"Connection lost, retrying: {e}")
            connected, _ = self.connect()
            if connected:
                try:
                    self.server_conn.send_message(msg)
                    return True, "Sent (Reconnected)"
                except Exception as final_e:
                    return False, str(final_e)
            return False, str(e)

    def quit(self):
        """Cleanly closes the connection."""
        if self.server_conn:
            try:
                self.server_conn.quit()
            except:
                try: self.server_conn.close()
                except: pass
            self.server_conn = None

    def test_connection(self):
        """One-off test."""
        success, msg = self.connect()
        self.quit()
        return success, msg
    
    # Backward compatibility for existing synchronous single-calls
    def send_email_sync(self, to_email, subject, html_content, unsubscribe_url=None):
        connected, msg = self.connect()
        if not connected: return False, msg
        success, msg = self.send_message_existing_conn(to_email, subject, html_content, unsubscribe_url)
        self.quit()
        return success, msg
