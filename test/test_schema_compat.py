from app import _build_missing_smtp_column_statements


def test_build_missing_smtp_columns_for_postgres():
    existing = {"id", "profile_name", "server"}
    statements = _build_missing_smtp_column_statements(existing, "postgresql")
    assert "ALTER TABLE smtp_server ADD COLUMN IF NOT EXISTS cc_emails TEXT" in statements
    assert "ALTER TABLE smtp_server ADD COLUMN IF NOT EXISTS bcc_emails TEXT" in statements


def test_build_missing_smtp_columns_none_missing():
    existing = {"id", "cc_emails", "bcc_emails"}
    statements = _build_missing_smtp_column_statements(existing, "postgresql")
    assert statements == []


def test_build_missing_smtp_columns_non_postgres():
    existing = {"id", "profile_name"}
    statements = _build_missing_smtp_column_statements(existing, "sqlite")
    assert "ALTER TABLE smtp_server ADD COLUMN cc_emails TEXT" in statements
    assert "ALTER TABLE smtp_server ADD COLUMN bcc_emails TEXT" in statements
