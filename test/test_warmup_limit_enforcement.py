from app.tasks import _check_profile_send_allowed, _record_profile_send_success


class DummyProfile:
    def __init__(self, *, can_send_result=True, warmup_enabled=False, sent_today=0, daily_limit=500, hourly_limit=100, sent_this_hour=0, warmup_limit=10):
        self._can_send_result = can_send_result
        self.warmup_enabled = warmup_enabled
        self.sent_today = sent_today
        self.daily_limit = daily_limit
        self.hourly_limit = hourly_limit
        self.sent_this_hour = sent_this_hour
        self._warmup_limit = warmup_limit
        self.increment_called = 0

    def can_send(self):
        return self._can_send_result

    def get_warmup_limit(self):
        return self._warmup_limit

    def increment_sent_count(self):
        self.increment_called += 1
        self.sent_today += 1
        self.sent_this_hour += 1


def test_warmup_limit_reached_returns_error_message():
    profile = DummyProfile(
        can_send_result=False,
        warmup_enabled=True,
        sent_today=10,
        warmup_limit=10,
    )
    ok, msg = _check_profile_send_allowed(profile)
    assert ok is False
    assert "Warmup limit reached" in msg


def test_daily_limit_reached_returns_error_message():
    profile = DummyProfile(
        can_send_result=False,
        warmup_enabled=False,
        sent_today=500,
        daily_limit=500,
    )
    ok, msg = _check_profile_send_allowed(profile)
    assert ok is False
    assert "Daily limit reached" in msg


def test_record_profile_send_success_increments_counts():
    profile = DummyProfile()
    _record_profile_send_success(profile)
    assert profile.increment_called == 1
    assert profile.sent_today == 1
    assert profile.sent_this_hour == 1
