from config import normalize_redis_url


def test_render_redis_url_uses_rediss_scheme():
    assert normalize_redis_url("redis://example.com:6379/0", is_render_env=True) == "rediss://example.com:6379/0"


def test_non_render_keeps_redis_scheme():
    assert normalize_redis_url("redis://example.com:6379/0", is_render_env=False) == "redis://example.com:6379/0"


def test_render_keeps_rediss_scheme():
    assert normalize_redis_url("rediss://example.com:6379/0", is_render_env=True) == "rediss://example.com:6379/0"


def test_render_preserves_empty_or_none_values():
    assert normalize_redis_url("", is_render_env=True) == ""
    assert normalize_redis_url(None, is_render_env=True) is None
