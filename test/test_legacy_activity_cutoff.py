from app import create_app
from app.main.routes import get_legacy_activity_cutoff


def test_get_legacy_activity_cutoff_uses_config_value():
    app = create_app("testing")
    with app.app_context():
        app.config["LEGACY_ACTIVITY_CUTOFF"] = "2026-03-05T12:34:56"
        cutoff = get_legacy_activity_cutoff()
        assert cutoff.year == 2026
        assert cutoff.month == 3
        assert cutoff.day == 5
        assert cutoff.hour == 12
        assert cutoff.minute == 34
        assert cutoff.second == 56


def test_get_legacy_activity_cutoff_falls_back_on_invalid_value():
    app = create_app("testing")
    with app.app_context():
        app.config["LEGACY_ACTIVITY_CUTOFF"] = "not-a-date"
        cutoff = get_legacy_activity_cutoff()
        assert cutoff.isoformat().startswith("2026-03-05T00:00:00")

