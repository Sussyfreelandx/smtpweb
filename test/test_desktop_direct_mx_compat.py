from email.mime.text import MIMEText

from app.core_logic.desktop_direct_mx_compat import resolve_mx_hosts, send_via_direct_mx


def test_resolve_mx_hosts_fallback_without_dns(monkeypatch):
    monkeypatch.setattr("app.core_logic.desktop_direct_mx_compat.DNSPYTHON_AVAILABLE", False)
    assert resolve_mx_hosts("example.com") == ["example.com"]


def test_send_via_direct_mx_invalid_recipient():
    msg = MIMEText("hello")
    ok, status = send_via_direct_mx(message=msg, to_email="invalid-email")
    assert ok is False
    assert "Invalid recipient domain" in status


def test_send_via_direct_mx_success(monkeypatch):
    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.sent = False
            self.starttls_context = None

        def ehlo(self, *_args, **_kwargs):
            return 250, b"OK"

        def has_extn(self, ext):
            return ext == "STARTTLS"

        def starttls(self, context=None):
            self.starttls_context = context
            return

        def sendmail(self, sender, recipients, message):
            self.sent = True

        def quit(self):
            return

    monkeypatch.setattr("app.core_logic.desktop_direct_mx_compat.resolve_mx_hosts", lambda _domain: ["mx1.example.com"])
    created = {}

    def smtp_factory(*args, **kwargs):
        obj = FakeSMTP(*args, **kwargs)
        created["smtp"] = obj
        return obj

    monkeypatch.setattr("app.core_logic.desktop_direct_mx_compat.smtplib.SMTP", smtp_factory)

    msg = MIMEText("hello")
    msg["From"] = "sender@example.com"
    msg["To"] = "target@example.net"

    ok, status = send_via_direct_mx(message=msg, to_email="target@example.net")
    assert ok is True
    assert "Sent via direct MX" in status
    assert created["smtp"].starttls_context is not None
