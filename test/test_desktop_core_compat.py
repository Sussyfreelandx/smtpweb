import smtplib
from email.mime.text import MIMEText

from app.core_logic.desktop_core_compat import detect_tls_mode, universal_smtp_send


class FakeSMTP:
    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_user = None
        self.sendmail_args = None
        self.send_message_called = False
        self.should_disconnect = False
        self.support_starttls = True

    def ehlo(self):
        return 250, b"OK"

    def has_extn(self, ext):
        return ext == 'STARTTLS' and self.support_starttls

    def starttls(self, context=None):
        self.starttls_called = True
        if self.should_disconnect:
            raise smtplib.SMTPServerDisconnected("closed")

    def login(self, user, password):
        self.login_user = user

    def sendmail(self, envelope_from, recipients, msg):
        self.sendmail_args = (envelope_from, recipients, msg)

    def send_message(self, msg, to_addrs=None):
        self.send_message_called = True
        self.sendmail_args = ("send_message", to_addrs, msg)

    def quit(self):
        return


def test_detect_tls_mode():
    assert detect_tls_mode(465) == (True, False)
    assert detect_tls_mode(587) == (False, True)
    assert detect_tls_mode(2525) == (False, True)
    assert detect_tls_mode(25) == (False, False)
    assert detect_tls_mode(25, ssl_enabled=True) == (True, False)


def test_universal_smtp_send_adds_reply_headers_and_envelope(monkeypatch):
    fake = FakeSMTP("smtp.example.com", 587)
    monkeypatch.setattr("app.core_logic.desktop_core_compat.smtplib.SMTP", lambda *a, **kw: fake)

    msg = MIMEText("hello", "plain", "utf-8")
    msg["Subject"] = "Status Update"
    msg["From"] = "Sender <sender@example.com>"
    msg["To"] = "to@example.com"
    msg["Cc"] = "copy@example.com"

    ok, status = universal_smtp_send(
        host="smtp.example.com",
        port=587,
        username="sender@example.com",
        password="secret",
        message=msg,
        use_starttls=True,
        envelope_from="bounce@example.com",
        in_reply_to="<message-id-1@example.com>",
        references="<message-id-0@example.com>",
    )

    assert ok is True
    assert "Sent successfully" in status
    assert msg["In-Reply-To"] == "<message-id-1@example.com>"
    assert msg["References"] == "<message-id-0@example.com>"
    assert str(msg["Subject"]).startswith("Re:")
    assert fake.starttls_called is True
    assert fake.sendmail_args is not None
    assert fake.sendmail_args[0] == "bounce@example.com"
    assert set(fake.sendmail_args[1]) == {"to@example.com", "copy@example.com"}


def test_universal_smtp_send_does_not_duplicate_re_prefix(monkeypatch):
    fake = FakeSMTP("smtp.example.com", 587)
    monkeypatch.setattr("app.core_logic.desktop_core_compat.smtplib.SMTP", lambda *a, **kw: fake)

    msg = MIMEText("hello", "plain", "utf-8")
    msg["Subject"] = "RE: Existing Thread"
    msg["From"] = "Sender <sender@example.com>"
    msg["To"] = "to@example.com"

    ok, status = universal_smtp_send(
        host="smtp.example.com",
        port=587,
        username="sender@example.com",
        password="secret",
        message=msg,
        use_starttls=True,
        in_reply_to="<message-id-1@example.com>",
    )

    assert ok is True
    assert "Sent successfully" in status
    assert str(msg["Subject"]) == "RE: Existing Thread"


def test_universal_smtp_send_starttls_fallback(monkeypatch):
    calls = {"count": 0}

    def factory(*args, **kwargs):
        obj = FakeSMTP(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            obj.should_disconnect = True
        return obj

    monkeypatch.setattr("app.core_logic.desktop_core_compat.smtplib.SMTP", factory)

    msg = MIMEText("hello")
    msg["From"] = "sender@example.com"
    msg["To"] = "to@example.com"
    msg["Subject"] = "Hello"

    ok, status = universal_smtp_send(
        host="smtp.example.com",
        port=587,
        username="sender@example.com",
        password="secret",
        message=msg,
        use_starttls=True,
    )

    assert ok is True
    assert "STARTTLS fallback" in status
    assert calls["count"] == 2


def test_universal_smtp_send_without_starttls_extension(monkeypatch):
    fake = FakeSMTP("smtp.example.com", 587)
    fake.support_starttls = False
    monkeypatch.setattr("app.core_logic.desktop_core_compat.smtplib.SMTP", lambda *a, **kw: fake)

    msg = MIMEText("hello")
    msg["From"] = "sender@example.com"
    msg["To"] = "to@example.com"
    msg["Subject"] = "Hello"

    ok, status = universal_smtp_send(
        host="smtp.example.com",
        port=587,
        username="sender@example.com",
        password="secret",
        message=msg,
        use_starttls=True,
    )

    assert ok is True
    assert "Sent successfully" in status
    assert fake.starttls_called is False


def test_universal_smtp_send_uses_implicit_ssl_when_enabled(monkeypatch):
    fake = FakeSMTP("smtp.example.com", 465)
    called = {"smtp": 0, "smtpssl": 0}

    def smtp_factory(*args, **kwargs):
        called["smtp"] += 1
        return fake

    def smtpssl_factory(*args, **kwargs):
        called["smtpssl"] += 1
        return fake

    monkeypatch.setattr("app.core_logic.desktop_core_compat.smtplib.SMTP", smtp_factory)
    monkeypatch.setattr("app.core_logic.desktop_core_compat.smtplib.SMTP_SSL", smtpssl_factory)

    msg = MIMEText("hello")
    msg["From"] = "sender@example.com"
    msg["To"] = "to@example.com"
    msg["Subject"] = "Hello"

    ok, status = universal_smtp_send(
        host="smtp.example.com",
        port=465,
        username="sender@example.com",
        password="secret",
        message=msg,
        use_ssl=True,
    )

    assert ok is True
    assert "Sent successfully" in status
    assert called["smtp"] == 0
    assert called["smtpssl"] == 1
