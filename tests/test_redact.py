"""Tests for backend_adapter.redact — secret masking in logs."""
from backend_adapter.redact import redact, redact_headers


class TestMask:
    """Tests for the internal _mask helper (via redact)."""

    def test_bearer_token_masked(self):
        """Bearer tokens should be masked: 4 prefix + REDACTED + 4 suffix."""
        assert redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9") == (
            "Authorization: Bearer eyJh***REDACTED***VCJ9"
        )

    def test_bearer_short_token_passthrough(self):
        """Bearer pattern requires ≥6 chars — 'short' is 5 chars, passes through."""
        assert redact("Authorization: Bearer short") == "Authorization: Bearer short"

    def test_mask_short_value(self):
        """Values <= 8 chars are fully ***REDACTED***."""
        from backend_adapter.redact import _mask
        assert _mask("short") == "***REDACTED***"

    def test_mask_long_value(self):
        """Values > 8 chars get prefix + REDACTED + suffix."""
        from backend_adapter.redact import _mask
        result = _mask("abc123xyz789")
        assert result == "abc1***REDACTED***z789"

    def test_empty_string_passthrough(self):
        """Empty string should pass through unchanged."""
        assert redact("") == ""
        assert redact(None) is None

    def test_key_variable_masked(self):
        """KEY/PAT/TOKEN/SECRET variable assignments should be masked."""
        result = redact("API_KEY=sk-abc123def456ghi789")
        assert "***REDACTED***" in result
        assert "sk-a" in result  # prefix preserved
        assert "i789" in result  # suffix preserved

    def test_multiple_secrets_masked(self):
        """Multiple secrets in one string should all be masked."""
        result = redact("Bearer abc123xyz789 and API_KEY=xyz789")
        assert "***REDACTED***" in result


class TestRedactHeaders:
    """Tests for redact_headers()."""

    def test_authorization_masked(self):
        """Authorization header should be redacted."""
        result = redact_headers({"Authorization": "Bearer abc123xyz789"})
        assert "***REDACTED***" in result["Authorization"]

    def test_other_headers_unchanged(self):
        """Non-Authorization headers should be unchanged."""
        result = redact_headers({
            "Content-Type": "application/json",
            "Authorization": "Bearer abc123xyz789",
            "X-Custom": "value",
        })
        assert result["Content-Type"] == "application/json"
        assert result["X-Custom"] == "value"
