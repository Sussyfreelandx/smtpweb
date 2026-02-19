import pytest
import threading
import time

from app.core_logic.smtp_handler import SMTPHandler

class MockConn:
    def __init__(self):
        self.sent_messages = []
        self.closed = False
    def ehlo(self):
        return
    def noop(self):
        return
    def has_extn(self, ext):
        return False
    def starttls(self, context=None):
        return
    def login(self, user, password):
        return
    def send_message(self, msg):
        # Simulate sending
        self.sent_messages.append(msg)
    def quit(self):
        self.closed = True
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass

def test_send_email_sync(monkeypatch):
    # Prepare a minimal smtp_config
    smtp_config = {
        'server': 'smtp.test.local',
        'port': 587,
        'username': 'user@test.local',
        'password': 'secret',
        'use_tls': True,
        'use_ssl': False,
        'sender_name': 'Tester',
        'sender_email': 'user@test.local'
    }

    handler = SMTPHandler(smtp_config)

    # Monkeypatch ProxySMTP to return a MockConn
    import app.core_logic.smtp_handler as smtp_module
    monkeypatch.setattr(smtp_module, "ProxySMTP", lambda *a, **kw: MockConn())

    success1, msg1 = handler.send_email_sync("a@example.com", "Subject 1", "<p>Hello</p>")
    success2, msg2 = handler.send_email_sync("b@example.com", "Subject 2", "<p>Hi</p>")

    assert success1 is True
    assert success2 is True

def test_send_bulk_threaded_works(monkeypatch):
    smtp_config = {
        'server': 'smtp.test.local',
        'port': 587,
        'username': 'user@test.local',
        'password': 'secret',
        'use_tls': True,
        'use_ssl': False,
        'sender_name': 'Tester',
        'sender_email': 'user@test.local'
    }

    handler = SMTPHandler(smtp_config)

    import app.core_logic.smtp_handler as smtp_module
    monkeypatch.setattr(smtp_module, "ProxySMTP", lambda *a, **kw: MockConn())

    tasks = [
        {'to_email': 'a@example.com', 'subject': '1', 'html_content': '<p>1</p>'},
        {'to_email': 'b@example.com', 'subject': '2', 'html_content': '<p>2</p>'},
        {'to_email': 'c@example.com', 'subject': '3', 'html_content': '<p>3</p>'},
    ]

    results = handler.send_bulk_threaded(tasks, max_workers=2)
    assert isinstance(results, list)
    for r in results:
        assert 'email' in r
        assert 'success' in r