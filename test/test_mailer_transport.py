from app.core_logic.mailer_transport import APIMailerTransport, DirectMXMailerTransport, SMTPMailerTransport, create_mailer_transport


def test_factory_returns_smtp_transport():
    transport = create_mailer_transport({
        "transport": "smtp",
        "server": "smtp.example.com",
        "username": "user@example.com",
        "password": "secret",
    })
    assert isinstance(transport, SMTPMailerTransport)


def test_factory_returns_api_transport():
    transport = create_mailer_transport({
        "transport": "api",
        "provider": "google",
        "username": "user@example.com",
        "password": "token",
    })
    assert isinstance(transport, APIMailerTransport)


def test_factory_returns_direct_mx_transport():
    transport = create_mailer_transport({
        "transport": "direct_mx",
        "sender_email": "user@example.com",
    })
    assert isinstance(transport, DirectMXMailerTransport)


def test_direct_mx_transport_send(monkeypatch):
    captured = {}

    def mock_send_via_direct_mx(**kwargs):
        captured.update(kwargs)
        return True, "Sent via direct MX (mx1.example.com)"

    monkeypatch.setattr("app.core_logic.mailer_transport.send_via_direct_mx", mock_send_via_direct_mx)

    transport = DirectMXMailerTransport({
        "transport": "direct_mx",
        "sender_email": "user@example.com",
    })
    ok, msg = transport.send_email(
        to_email="target@example.net",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
        bcc_emails=["audit@example.net"],
    )
    assert ok is True
    assert "direct MX" in msg
    assert captured["to_email"] == "target@example.net"
    assert captured["additional_recipients"] == ["audit@example.net"]


def test_smtp_transport_uses_desktop_universal_send(monkeypatch):
    captured = {}

    def mock_universal_smtp_send(**kwargs):
        captured.update(kwargs)
        return True, "Sent successfully"

    monkeypatch.setattr("app.core_logic.mailer_transport.universal_smtp_send", mock_universal_smtp_send)

    transport = SMTPMailerTransport({
        "server": "smtp.example.com",
        "port": 587,
        "username": "user@example.com",
        "password": "secret",
        "use_tls": True,
        "use_ssl": False,
        "sender_name": "User",
        "sender_email": "user@example.com",
        "envelope_from": "bounce@example.com",
        "in_reply_to": "<abc@example.com>",
        "references": "<root@example.com>",
        "cc_emails": ["cc-default@example.com"],
        "bcc_emails": ["bcc-default@example.com"],
    })

    ok, msg = transport.send_email(
        to_email="target@example.com",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
    )

    assert ok is True
    assert "Sent successfully" in msg
    assert captured["host"] == "smtp.example.com"
    assert captured["use_starttls"] is True
    assert captured["use_ssl"] is False
    assert captured["envelope_from"] == "bounce@example.com"
    assert captured["in_reply_to"] == "<abc@example.com>"
    assert captured["references"] == "<root@example.com>"
    assert captured["additional_recipients"] == ["bcc-default@example.com"]
    assert captured["message"].get("Cc") == "cc-default@example.com"


def test_google_api_send_email(monkeypatch):
    class Resp:
        status_code = 200
        text = "ok"

    captured = {}

    def mock_post(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Resp()

    monkeypatch.setattr("app.core_logic.mailer_transport.requests.post", mock_post)

    transport = APIMailerTransport({
        "transport": "api",
        "provider": "google",
        "username": "user@example.com",
        "password": "token",
        "sender_email": "user@example.com",
        "cc_emails": ["cc@example.com"],
        "bcc_emails": ["bcc@example.com"],
    })

    ok, msg = transport.send_email(
        to_email="target@example.com",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
    )
    assert ok is True
    assert "Google API" in msg
    assert captured["args"][0].endswith("/messages/send")
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer token"
    assert "raw" in captured["kwargs"]["json"]


def test_microsoft_api_send_email(monkeypatch):
    class Resp:
        status_code = 202
        text = "accepted"

    captured = {}

    def mock_post(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Resp()

    monkeypatch.setattr("app.core_logic.mailer_transport.requests.post", mock_post)

    transport = APIMailerTransport({
        "transport": "api",
        "provider": "microsoft",
        "username": "user@example.com",
        "password": "token",
        "sender_email": "user@example.com",
        "cc_emails": ["cc@example.com"],
        "bcc_emails": ["bcc@example.com"],
    })

    ok, msg = transport.send_email(
        to_email="target@example.com",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
        custom_headers={"Message-ID": "<abc@example.com>"},
    )
    assert ok is True
    assert "Microsoft Graph" in msg
    assert captured["args"][0].endswith("/me/sendMail")
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer token"
    assert "message" in captured["kwargs"]["json"]
    message = captured["kwargs"]["json"]["message"]
    assert message["ccRecipients"][0]["emailAddress"]["address"] == "cc@example.com"
    assert message["bccRecipients"][0]["emailAddress"]["address"] == "bcc@example.com"
    assert message["internetMessageHeaders"][0]["name"] == "Message-ID"


def test_google_api_send_email_failure(monkeypatch):
    class Resp:
        status_code = 401
        text = "invalid token"

    def mock_post(*args, **kwargs):
        return Resp()

    monkeypatch.setattr("app.core_logic.mailer_transport.requests.post", mock_post)

    transport = APIMailerTransport({
        "transport": "api",
        "provider": "google",
        "username": "user@example.com",
        "password": "token",
        "sender_email": "user@example.com",
    })

    ok, msg = transport.send_email(
        to_email="target@example.com",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
    )
    assert ok is False
    assert "Google API error" in msg


def test_microsoft_api_send_email_failure(monkeypatch):
    class Resp:
        status_code = 401
        text = "invalid token"

    def mock_post(*args, **kwargs):
        return Resp()

    monkeypatch.setattr("app.core_logic.mailer_transport.requests.post", mock_post)

    transport = APIMailerTransport({
        "transport": "api",
        "provider": "microsoft",
        "username": "user@example.com",
        "password": "token",
        "sender_email": "user@example.com",
    })

    ok, msg = transport.send_email(
        to_email="target@example.com",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
    )
    assert ok is False
    assert "Microsoft Graph error" in msg
