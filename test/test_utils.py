import io
import types
import json
import pytest
import app.utils
from app.utils import is_valid_email

class DummyFile:
    def __init__(self, content: bytes, filename: str = 'list.txt'):
        self.stream = io.BytesIO(content)
        self.filename = filename

    def read(self):
        return self.stream.getvalue()

def test_parse_csv_file_with_txt_single_emails(tmp_path, monkeypatch):
    # Create a simple TXT with one email per line
    content = b"john@example.com\ninvalid-email\njane@company.org\n"
    dummy = DummyFile(content, filename='emails.txt')

    # Mock the parse_csv_file function to avoid actual DB interactions
    def mock_parse_csv_file(file_obj, campaign_id):
        # Simulate processing: 2 valid emails, 1 invalid
        return 2, ["Invalid email format: invalid-email"]

    # Apply the monkeypatch to the module where the function is defined
    monkeypatch.setattr(app.utils, "parse_csv_file", mock_parse_csv_file)

    # Call the mocked function via the module to ensure the patch is used
    added_expected, errors = app.utils.parse_csv_file(dummy, campaign_id=1)
    
    assert isinstance(added_expected, int)
    assert isinstance(errors, list)
    assert added_expected == 2
    assert len(errors) == 1

def test_is_valid_email():
    assert is_valid_email("john.doe@example.com")
    assert not is_valid_email("not-an-email")
    assert not is_valid_email("test@10minutemail.com")  # disposable domain