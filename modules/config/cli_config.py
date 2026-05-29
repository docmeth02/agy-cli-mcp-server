"""
Main configuration interface for Antigravity CLI MCP Server.

This module consolidates all configuration from environment variables
and provides a unified interface for the rest of the application.
"""
import os
from typing import Optional

# ============================================================================
# Core Configuration (with backward-compatible env var fallbacks)
# ============================================================================

CLI_TIMEOUT = int(os.getenv("CLI_TIMEOUT", os.getenv("GEMINI_TIMEOUT", "300")))
CLI_COMMAND_PATH = os.getenv("CLI_COMMAND_PATH", os.getenv("GEMINI_COMMAND_PATH", "agy"))
CLI_LOG_LEVEL = os.getenv("CLI_LOG_LEVEL", os.getenv("GEMINI_LOG_LEVEL", "INFO")).upper()
CLI_OUTPUT_FORMAT = os.getenv("CLI_OUTPUT_FORMAT", os.getenv("GEMINI_OUTPUT_FORMAT", "json"))

# Optional override for agy's own diagnostic log file (language-server startup,
# warnings, update checks). Set CLI_LOG_FILE to a real, writable path to keep
# that noise off stdout and make the error-pattern scan more robust. Disabled
# by default: agy already keeps stdout clean in --print mode, and the null
# device (os.devnull) is NOT a safe value here — agy hangs when --log-file
# points at /dev/null. Must be a regular file path if set.
CLI_LOG_FILE = os.getenv("CLI_LOG_FILE", "")

# Extra seconds added to agy's internal --print-timeout on top of the Python
# supervisor timeout. Python stays the authoritative supervisor (it kills the
# subprocess at exactly `timeout`); agy's own timeout sits just above as a
# safety net so it never preempts the configured budget, yet still self-aborts
# if the supervisor's kill ever fails.
CLI_PRINT_TIMEOUT_GRACE = int(os.getenv("CLI_PRINT_TIMEOUT_GRACE", "30"))

# ============================================================================
# Retry Configuration
# ============================================================================

RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))
RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "30.0"))

# ============================================================================
# Tool-Specific Character Limits
# ============================================================================
# Kept under original GEMINI_* names for backward compatibility of callers.

GEMINI_PROMPT_LIMIT = int(os.getenv("GEMINI_PROMPT_LIMIT", "100000"))
GEMINI_SANDBOX_LIMIT = int(os.getenv("GEMINI_SANDBOX_LIMIT", "200000"))
GEMINI_SUMMARIZE_LIMIT = int(os.getenv("GEMINI_SUMMARIZE_LIMIT", "400000"))
GEMINI_SUMMARIZE_FILES_LIMIT = int(os.getenv("GEMINI_SUMMARIZE_FILES_LIMIT", "800000"))
GEMINI_EVAL_LIMIT = int(os.getenv("GEMINI_EVAL_LIMIT", "500000"))
GEMINI_REVIEW_LIMIT = int(os.getenv("GEMINI_REVIEW_LIMIT", "300000"))
GEMINI_VERIFY_LIMIT = int(os.getenv("GEMINI_VERIFY_LIMIT", "800000"))
GEMINI_COLLABORATION_LIMIT = int(os.getenv("GEMINI_COLLABORATION_LIMIT", "500000"))
GEMINI_CODE_REVIEW_LIMIT = int(os.getenv("GEMINI_CODE_REVIEW_LIMIT", "300000"))
GEMINI_EXTRACT_STRUCTURED_LIMIT = int(os.getenv("GEMINI_EXTRACT_STRUCTURED_LIMIT", "200000"))
GEMINI_GIT_DIFF_LIMIT = int(os.getenv("GEMINI_GIT_DIFF_LIMIT", "150000"))
GEMINI_CONTENT_COMPARISON_LIMIT = int(os.getenv("GEMINI_CONTENT_COMPARISON_LIMIT", "400000"))

# ============================================================================
# Model Configuration (no-ops for backward compatibility)
# ============================================================================
# Antigravity CLI does not support --model. These symbols are kept as stubs
# so existing imports do not break during the transition.

DEFAULT_MODEL = os.getenv("GEMINI_DEFAULT_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
ENABLE_FALLBACK = os.getenv("GEMINI_ENABLE_FALLBACK", "true").lower() == "true"

# ============================================================================
# Rate Limiting Configuration
# ============================================================================

GEMINI_RATE_LIMIT_REQUESTS = int(os.getenv("GEMINI_RATE_LIMIT_REQUESTS", "100"))
GEMINI_RATE_LIMIT_WINDOW = int(os.getenv("GEMINI_RATE_LIMIT_WINDOW", "60"))

# ============================================================================
# Security Configuration
# ============================================================================

JSONRPC_MAX_REQUEST_SIZE = int(os.getenv("JSONRPC_MAX_REQUEST_SIZE", "1048576"))
JSONRPC_MAX_NESTING_DEPTH = int(os.getenv("JSONRPC_MAX_NESTING_DEPTH", "10"))
JSONRPC_STRICT_MODE = os.getenv("JSONRPC_STRICT_MODE", "true").lower() == "true"


def get_config_summary() -> dict:
    """Get a summary of current configuration."""
    return {
        "core": {
            "timeout": CLI_TIMEOUT,
            "command_path": CLI_COMMAND_PATH,
            "log_level": CLI_LOG_LEVEL,
            "output_format": CLI_OUTPUT_FORMAT,
        },
        "limits": {
            "prompt": GEMINI_PROMPT_LIMIT,
            "sandbox": GEMINI_SANDBOX_LIMIT,
            "summarize": GEMINI_SUMMARIZE_LIMIT,
            "summarize_files": GEMINI_SUMMARIZE_FILES_LIMIT,
            "eval": GEMINI_EVAL_LIMIT,
            "review": GEMINI_REVIEW_LIMIT,
            "verify": GEMINI_VERIFY_LIMIT,
            "collaboration": GEMINI_COLLABORATION_LIMIT,
        },
        "models": {
            "note": "Antigravity CLI manages models internally. "
                    "No explicit model selection available.",
            "default": DEFAULT_MODEL,
            "fallback": FALLBACK_MODEL,
            "fallback_enabled": ENABLE_FALLBACK,
        },
        "rate_limiting": {
            "requests": GEMINI_RATE_LIMIT_REQUESTS,
            "window_seconds": GEMINI_RATE_LIMIT_WINDOW,
        },
    }
