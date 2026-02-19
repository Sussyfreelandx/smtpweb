import pytest
from unittest.mock import MagicMock

from app.core_logic.smtp_handler import SMTPHandler, SMTPRotationManager

def test_rotation_selects_profiles():
    # Build three profiles with different usage
    profiles = [
        {'id': 1, 'username': 'u1', 'password': 'p', 'server': 'smtp.test.local', 'port': 587, 'daily_limit': 100, 'sent_today': 10, 'priority': 2},
        {'id': 2, 'username': 'u2', 'password': 'p', 'server': 'smtp.test.local', 'port': 587, 'daily_limit': 100, 'sent_today': 50, 'priority': 1},
        {'id': 3, 'username': 'u3', 'password': 'p', 'server': 'smtp.test.local', 'port': 587, 'daily_limit': 100, 'sent_today': 0, 'priority': 3},
    ]

    rot = SMTPRotationManager(profiles)

    # First selection should yield a handler (not None)
    h1, err1 = rot.get_next_handler()
    assert h1 is not None
    assert err1 is None

    h2, err2 = rot.get_next_handler()
    assert h2 is not None
    assert err2 is None

    # Close handlers
    rot.close_all()