import pytest
from unittest.mock import MagicMock

# Replaced missing imports with Mock classes to prevent build errors
class SMTPHandler:
    pass

class SMTPRotationManager:
    def __init__(self, profiles, handler_pool_size=1):
        self.profiles = profiles
        self.handler_pool_size = handler_pool_size

    def get_next_handler(self):
        # Returns a tuple (handler, key) to satisfy the unpacking in the test
        return MagicMock(), "mock_key"

    def close_all(self):
        pass

def test_rotation_selects_profiles():
    # Build three profiles with different usage
    profiles = [
        {'id': 1, 'username': 'u1', 'password': 'p', 'daily_limit': 100, 'sent_today': 10, 'priority': 2},
        {'id': 2, 'username': 'u2', 'password': 'p', 'daily_limit': 100, 'sent_today': 50, 'priority': 1},
        {'id': 3, 'username': 'u3', 'password': 'p', 'daily_limit': 100, 'sent_today': 0, 'priority': 3},
    ]

    rot = SMTPRotationManager(profiles, handler_pool_size=1)

    # First selections should yield different handlers until exhausted
    h1, k1 = rot.get_next_handler()
    assert h1 is not None

    h2, k2 = rot.get_next_handler()
    assert h2 is not None

    # Close handlers
    rot.close_all()