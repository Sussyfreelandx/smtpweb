from app.tasks import _apply_seed_spam_pause


class DummyCampaign:
    def __init__(self, status):
        self.status = status
        self.status_message = ""


def test_apply_seed_spam_pause_pauses_only_sending_campaigns():
    campaigns = [DummyCampaign("Sending"), DummyCampaign("Paused"), DummyCampaign("Completed")]
    paused = _apply_seed_spam_pause(
        campaigns,
        spam_rate=60.0,
        pause_message="Auto-paused: high seed spam rate detected",
    )
    assert paused == 1
    assert campaigns[0].status == "Paused"
    assert campaigns[0].status_message == "Auto-paused: high seed spam rate detected"
    # Pre-paused campaigns should remain unchanged.
    assert campaigns[1].status == "Paused"
    assert campaigns[2].status == "Completed"


def test_apply_seed_spam_pause_no_pause_below_threshold():
    campaigns = [DummyCampaign("Sending")]
    paused = _apply_seed_spam_pause(
        campaigns,
        spam_rate=20.0,
        pause_message="Auto-paused: high seed spam rate detected",
    )
    assert paused == 0
    assert campaigns[0].status == "Sending"
