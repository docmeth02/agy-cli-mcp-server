"""
Unit tests for the security framework modules.

These tests verify the credential sanitizer and security monitor
without requiring a real agy installation.
"""
import time

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
