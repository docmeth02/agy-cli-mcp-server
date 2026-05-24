"""
Pure MCP tool implementations for core Antigravity CLI tools.

This module contains the core implementations that are imported by mcp_server.py.
"""
import json
import logging
from typing import Optional

from modules.utils.cli_utils import (
    execute_cli_with_retry,
    extract_file_refs,
    _build_cli_args,
    CLIExecutionError,
    CLITimeoutError,
    CLIRateLimitError,
)
from modules.config.cli_config import (
    GEMINI_PROMPT_LIMIT,
    DEFAULT_MODEL,
)

logger = logging.getLogger(__name__)


async def execute_prompt(
    prompt: str,
    model: Optional[str] = None,
    sandbox: bool = False,
    debug: bool = False
) -> dict:
    """
    Execute a prompt with the Antigravity CLI.

    Args:
        prompt: The prompt to send
        model: Model to use (ignored; kept for backward compatibility)
        sandbox: Whether to use sandbox mode
        debug: Whether to enable debug output (ignored for agy)

    Returns:
        Execution result dictionary
    """
    if len(prompt) > GEMINI_PROMPT_LIMIT:
        return {
            "status": "error",
            "error": f"Prompt exceeds limit of {GEMINI_PROMPT_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        }

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(
        prompt=cleaned_prompt,
        sandbox=sandbox,
        debug=debug,
        files=files
    )

    try:
        result = await execute_cli_with_retry(args)
        if model is not None and model != DEFAULT_MODEL:
            result["model_ignored"] = True
        return result
    except CLITimeoutError as e:
        return {"status": "error", "error": str(e), "error_code": "TIMEOUT"}
    except CLIRateLimitError as e:
        return {"status": "error", "error": str(e), "error_code": "RATE_LIMIT"}
    except CLIExecutionError as e:
        return {"status": "error", "error": str(e), "error_code": "EXECUTION_ERROR"}
    except Exception as e:
        logger.error(f"Unexpected error in execute_prompt: {e}")
        return {"status": "error", "error": str(e), "error_code": "INTERNAL_ERROR"}


def validate_prompt_length(prompt: str, limit: int) -> tuple[bool, str]:
    """
    Validate that a prompt is within the allowed length.

    Args:
        prompt: The prompt to validate
        limit: Character limit

    Returns:
        Tuple of (is_valid, error_message)
    """
    if len(prompt) > limit:
        return False, f"Input exceeds limit of {limit:,} characters (got {len(prompt):,})"

    return True, ""


def build_prompt_with_context(
    base_prompt: str,
    context: Optional[str] = None,
    requirements: Optional[str] = None,
    additional_params: Optional[dict] = None
) -> str:
    """
    Build a full prompt with optional context and requirements.

    Args:
        base_prompt: The main prompt content
        context: Optional context information
        requirements: Optional requirements
        additional_params: Additional parameters to include

    Returns:
        Complete prompt string
    """
    parts = [base_prompt]

    if context:
        parts.append(f"\n\nContext:\n{context}")

    if requirements:
        parts.append(f"\n\nRequirements:\n{requirements}")

    if additional_params:
        for key, value in additional_params.items():
            if value:
                parts.append(f"\n\n{key.replace('_', ' ').title()}:\n{value}")

    return "".join(parts)
