import smtplib
import ssl
import logging
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr, formatdate, make_msgid
from email.header import Header
import os

log = logging.getLogger(__name__)


class SMTPHandler:
    def __init__(self, smtp_config):
        self.smtp_server = smtp_config. get('server')
        self.smtp_port = int(smtp_config. get('port', 587))
        self.username = smtp_config. get('username')
        self.password = smtp_config.get('password')
        self.use_tls = smtp_config.get('use_tls', True)
        self.use_ssl = smtp_config.get('use_ssl', False)
        self.sender_name = smtp_config.get('sender_name', '')
        self.sender_email = smtp_config.get('sender_email') or self.username

    def _create_secure_ssl_context(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _html_to_text(self, html):
        if not html:
            return ""
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</(p|h[1-6]|li|div|tr)\s*>', '\n', text, flags=re. IGNORECASE)
        text = re.sub(r'<br\s*/? >', '\n', text, flags=re. IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\s+', ' ', text)
        lines = [line.strip() for line in text.split('\n')]
        return '\n'.join(line for line in lines if line).strip()

    def _create_mime_message(self, to_email, subject, html_content, plain_content=None, 
                             unsubscribe_url=None, attachments=None):
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
        msg_root['Message-ID'] = make_msgid()
        msg_root['MIME-Version'] = '1.0'

        if unsubscribe_url:
            msg_root. add_header('List-Unsubscribe', f'<{unsubscribe_url}>')
            msg_root.add_header('List-Unsubscribe-Post', 'List-Unsubscribe=One-Click')

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
                            part. set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header('Content-Disposition', f'attachment; filename="{os.path. basename(filepath)}"')
                        msg_root.attach(part)
                    except Exception as e:
                        log.error(f"Failed to attach {filepath}: {e}")

        return msg_root

    def test_connection(self):
        server = None
        try: 
            if not self.smtp_server or not self.username:
                return False, "SMTP configuration incomplete"

            if not self.password:
                return False, "Password not configured"

            context = self._create_secure_ssl_context()

            if self.use_ssl or self.smtp_port == 465:
                server = smtplib. SMTP_SSL(self.smtp_server, self. smtp_port, context=context, timeout=15)
            else:
                server = smtplib. SMTP(self. smtp_server, self.smtp_port, timeout=15)

            server.ehlo()

            if not self.use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                server. starttls(context=context)
                server.ehlo()

            server.login(self. username, self.password)
            server.quit()

            return True, "Connection successful!"

        except smtplib.SMTPAuthenticationError as e: 
            error_msg = e.smtp_error. decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            return False, f"Authentication Failed: {error_msg}"

        except smtplib.SMTPConnectError as e: 
            return False, f"Connection Error: {str(e)}"

        except smtplib. SMTPServerDisconnected as e:
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
                        unsubscribe_url=None, attachments=None):
        server = None
        try:
            if not self.password:
                return False, "Password not configured"

            mime_message = self._create_mime_message(
                to_email,
                subject,
                html_content,
                plain_content=plain_content,
                unsubscribe_url=unsubscribe_url,
                attachments=attachments
            )

            context = self._create_secure_ssl_context()

            if self.use_ssl or self. smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30)
            else:
                server = smtplib. SMTP(self.smtp_server, self. smtp_port, timeout=30)

            server.ehlo()

            if not self. use_ssl and self.use_tls and server.has_extn('STARTTLS'):
                server.starttls(context=context)
                server.ehlo()

            server.login(self. username, self.password)
            server. send_message(mime_message)
            server.quit()

            return True, "Sent"

        except smtplib.SMTPAuthenticationError as e:
            error_msg = e.smtp_error.decode() if isinstance(e. smtp_error, bytes) else str(e.smtp_error)
            log.error(f"SMTP Auth Error for {to_email}:  {error_msg}")
            return False, f"Authentication Error: {error_msg}"

        except smtplib.SMTPRecipientsRefused as e:
            log.error(f"SMTP Recipients Refused for {to_email}: {e}")
            return False, f"Recipient Refused: {to_email}"

        except smtplib.SMTPSenderRefused as e: 
            log.error(f"SMTP Sender Refused:  {e}")
            return False, "Sender Refused"

        except smtplib.SMTPDataError as e: 
            error_msg = e. smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            log.error(f"SMTP Data Error for {to_email}: {error_msg}")
            return False, f"Data Error: {error_msg}"

        except smtplib.SMTPServerDisconnected as e:
            log. error(f"SMTP Server Disconnected for {to_email}: {e}")
            return False, "Server Disconnected"

        except Exception as e:
            log.error(f"SMTP sending failed for {to_email}: {e}")
            return False, str(e)

        finally:
            if server:
                try:
                    server.close()
                except Exception: 
                    pass
