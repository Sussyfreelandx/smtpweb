from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_template_surfaces_phase_feature_modules():
    template_path = ROOT / "app" / "templates" / "dashboard.html"
    content = template_path.read_text(encoding="utf-8")

    assert "Feature Modules" in content
    assert "Campaign Composer" in content
    assert "Mailer Profiles" in content
    assert "Deliverability Tools" in content


def test_base_template_no_legacy_black_activity_log_ui():
    template_path = ROOT / "app" / "templates" / "base.html"
    content = template_path.read_text(encoding="utf-8")

    assert "LIVE ACTIVITY LOG" not in content
    assert "activity-log-container" not in content
