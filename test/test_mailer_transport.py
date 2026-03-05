from app.core_logic.mailer_transport import APIMailerTransport, SMTPMailerTransport, create_mailer_transport


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
    })

    ok, msg = transport.send_email(
        to_email="target@example.com",
        subject="Hello",
        html_content="<p>hi</p>",
        plain_content="hi",
    )
    assert ok is True
    assert "Microsoft Graph" in msg
    assert captured["args"][0].endswith("/me/sendMail")
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer token"
    assert "message" in captured["kwargs"]["json"]


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
