"""
Unit tests for the security framework modules.

These tests verify the credential sanitizer and security monitor
without requiring a real agy installation.
"""
import pytest
from security.credential_sanitizer import (
    sanitize_credentials,
    check_for_credentials,
    CredentialSanitizer,
)
from security.security_monitor import get_security_monitor, SecurityMonitor


class TestCredentialSanitizer:

    def test_google_api_key(self):
        text = "key=AIzaSyB_fake_key_1234567890123456789012"
        assert "AIza" not in sanitize_credentials(text)
        assert "[REDACTED" in sanitize_credentials(text)

    def test_openai_key(self):
        text = "sk-abcdefghijklmnopqrstuvwxyz1234567890abcd"
        assert "sk-" not in sanitize_credentials(text)

    def test_anthropic_key(self):
        text = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890abcdef"
        assert "sk-ant" not in sanitize_credentials(text)

    def test_aws_access_key(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        assert "AKIA" not in sanitize_credentials(text)

    def test_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
        result = sanitize_credentials(text)
        assert "eyJhbGci" not in result
        assert "Bearer [REDACTED]" in result or "[REDACTED" in result

    def test_password_pattern(self):
        text = 'password=mysecretpassword123'
        assert "mysecret" not in sanitize_credentials(text)

    def test_generic_secret(self):
        text = 'secret="my_super_secret_value"'
        assert "my_super" not in sanitize_credentials(text)

    def test_jwt_token(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        assert jwt not in sanitize_credentials(jwt)

    def test_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
        assert "PRIVATE KEY" not in sanitize_credentials(text)

    def test_clean_text_unchanged(self):
        text = "Hello world, this is a normal response."
        assert sanitize_credentials(text) == text

    def test_empty_string(self):
        assert sanitize_credentials("") == ""

    def test_check_for_credentials_true(self):
        assert check_for_credentials("password=secret123")

    def test_check_for_credentials_false(self):
        assert not check_for_credentials("just normal text")

    def test_superset_of_old_sanitize_output(self):
        """Verify credential_sanitizer catches everything old sanitize_output did."""
        old_patterns_samples = [
            "AIzaSyB_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "sk-abcdefghijklmnopqrstuvwxyz123456789012",
            "Bearer eyJtoken.payload.sig",
        ]
        for sample in old_patterns_samples:
            result = sanitize_credentials(sample)
            assert sample not in result, f"Failed to sanitize: {sample}"

    def test_custom_patterns(self):
        sanitizer = CredentialSanitizer(
            additional_patterns=[(r"MY_CUSTOM_\w+", "[CUSTOM_REDACTED]")]
        )
        assert "[CUSTOM_REDACTED]" in sanitizer.sanitize("key=MY_CUSTOM_SECRET")


class TestSecurityMonitor:

    def setup_method(self):
        self.monitor = SecurityMonitor()

    def test_record_event(self):
        event = self.monitor.record_event("test", "low", "unit_test", {"key": "val"})
        assert event.event_type == "test"
        assert event.severity == "low"
        assert event.source == "unit_test"

    def test_get_stats(self):
        self.monitor.record_event("test", "low", "unit_test")
        stats = self.monitor.get_stats()
        assert stats["total_events"] >= 1
        assert "by_severity" in stats

    def test_get_recent_events(self):
        self.monitor.record_event("a", "low", "test")
        self.monitor.record_event("b", "high", "test")
        events = self.monitor.get_recent_events(count=10)
        assert len(events) >= 2

    def test_severity_filter(self):
        self.monitor.record_event("low_event", "low", "test")
        self.monitor.record_event("high_event", "high", "test")
        high_only = self.monitor.get_recent_events(severity_filter="high")
        assert all(e.severity == "high" for e in high_only)

    def test_threat_level_normal(self):
        assert self.monitor.get_threat_level() == "normal"

    def test_threat_level_critical(self):
        self.monitor.record_event("attack", "critical", "test")
        assert self.monitor.get_threat_level() == "critical"

    def test_singleton_accessor(self):
        m1 = get_security_monitor()
        m2 = get_security_monitor()
        assert m1 is m2


class TestPrivateKeyPatternCost:
    """
    A permissive body makes every BEGIN marker rescan forward for an END, which is
    quadratic in the marker count. Sanitization is synchronous and runs outside
    the asyncio timeout, so one hostile payload would stall the whole server.
    A length bound alone is NOT sufficient — a single trailing END marker
    satisfies the literal prefilter and restores the full cost.
    """

    def test_repeated_begin_markers_with_trailing_end_stay_linear(self):
        import time
        from security.credential_sanitizer import sanitize_credentials

        payload = "-----BEGIN PRIVATE KEY-----" * 38_000 + "-----END PRIVATE KEY-----"
        start = time.perf_counter()
        sanitize_credentials(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"~1MB took {elapsed:.2f}s — superlinear"

    def test_repeated_begin_markers_without_end_stay_linear(self):
        import time
        from security.credential_sanitizer import sanitize_credentials

        payload = "-----BEGIN PRIVATE KEY-----" * 38_000
        start = time.perf_counter()
        sanitize_credentials(payload)
        assert time.perf_counter() - start < 1.0

    @pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "DSA "])
    def test_real_keys_still_redacted(self, kind):
        from security.credential_sanitizer import sanitize_credentials

        key = (
            f"-----BEGIN {kind}PRIVATE KEY-----\n"
            + "MIIEowIBAAKCAQEA" * 200
            + f"\n-----END {kind}PRIVATE KEY-----"
        )
        assert "[REDACTED_PRIVATE_KEY]" in sanitize_credentials(key)

    def test_lowercase_markers_redacted(self):
        from security.credential_sanitizer import sanitize_credentials
        key = "-----begin private key-----\nabc\n-----end private key-----"
        assert "[REDACTED_PRIVATE_KEY]" in sanitize_credentials(key)


class TestJwtPatternCost:
    """
    Without a leading token boundary, every third character of a base64 run like
    "eyJeyJeyJ..." starts a fresh JWT candidate (e, y and J are all in the
    character class) and each scans to end-of-string for a '.'. That is quadratic
    — 4.5s at 128KB, minutes at 1MB — and it runs synchronously outside the
    asyncio timeout, so one payload stalls the whole server. agy output is not
    length-capped, so this is reachable.
    """

    @pytest.mark.parametrize("size", [43_600, 350_000])
    def test_base64_run_stays_linear(self, size):
        import time
        from security.credential_sanitizer import sanitize_credentials

        start = time.perf_counter()
        sanitize_credentials("eyJ" * size)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"{size * 3 // 1024}KB took {elapsed:.2f}s — superlinear"

    def test_dot_in_payload_does_not_restore_cost(self):
        # A '.' prefilter would be defeated by a single dot; the boundary anchor
        # is what actually bounds this.
        import time
        from security.credential_sanitizer import sanitize_credentials

        start = time.perf_counter()
        sanitize_credentials("eyJ" * 100_000 + ".")
        assert time.perf_counter() - start < 1.0

    REAL_JWT = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    @pytest.mark.parametrize("wrap", [
        "{jwt}",
        "token is {jwt} end",
        '{{"t":"{jwt}"}}',
        "\n{jwt}",
        "Authorization={jwt}",
    ])
    def test_real_jwts_still_redacted(self, wrap):
        from security.credential_sanitizer import sanitize_credentials
        text = wrap.format(jwt=self.REAL_JWT)
        assert "[REDACTED_JWT]" in sanitize_credentials(text)


class TestPrivateKeyTypeCoverage:
    """OPENSSH is ssh-keygen's DEFAULT output format, and was matched by nothing."""

    @pytest.mark.parametrize("kind", [
        "", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ",
    ])
    def test_key_types_redacted(self, kind):
        from security.credential_sanitizer import sanitize_credentials
        key = (
            f"-----BEGIN {kind}PRIVATE KEY-----\n"
            + "MIIEowIBAAKCAQEA" * 50
            + f"\n-----END {kind}PRIVATE KEY-----"
        )
        assert "[REDACTED_PRIVATE_KEY]" in sanitize_credentials(key)

    def test_prefilter_is_keyed_by_pattern_not_replacement(self):
        # Keying on the replacement string let a caller-supplied additional
        # pattern reusing the same placeholder inherit the "-----END" gate and
        # silently skip its own redaction.
        from security.credential_sanitizer import CredentialSanitizer
        s = CredentialSanitizer(
            additional_patterns=[(r"CUSTOMSECRET-[0-9]+", "[REDACTED_PRIVATE_KEY]")]
        )
        # No "-----END" anywhere, so an inherited gate would skip this.
        assert "[REDACTED_PRIVATE_KEY]" in s.sanitize("value CUSTOMSECRET-12345 here")
