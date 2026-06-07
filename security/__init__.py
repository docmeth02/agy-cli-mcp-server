"""
Security framework for Antigravity CLI MCP Server.

Active modules:
- Credential sanitization (output scrubbing for API keys, tokens, secrets)
- Security monitoring (event recording and threat level tracking)

Additional modules (pattern_detector, security_validator, jsonrpc_validator)
are available but not wired into the MCP execution path to avoid false
positives on code-containing prompts.
"""
from .credential_sanitizer import sanitize_credentials, CredentialSanitizer

__all__ = [
    "sanitize_credentials",
    "CredentialSanitizer",
]
