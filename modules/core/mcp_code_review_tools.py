"""
Specialized code review and analysis tools.

This module implements the gemini_code_review, gemini_extract_structured,
and gemini_git_diff_review tools.
"""
import json
import logging
from typing import Optional

from modules.utils.cli_utils import (
    execute_cli_with_retry,
    extract_file_refs,
    _build_cli_args,
    validate_model,
    add_model_metadata,
    CLIExecutionError,
    CLITimeoutError,
    CLIRateLimitError,
)
from modules.config.cli_config import (
    GEMINI_CODE_REVIEW_LIMIT,
    GEMINI_EXTRACT_STRUCTURED_LIMIT,
    GEMINI_GIT_DIFF_LIMIT,
    get_task_model,
    get_task_timeout,
)

logger = logging.getLogger(__name__)


async def execute_code_review(
    code: str,
    language: Optional[str] = None,
    focus_areas: Optional[str] = None,
    severity_threshold: str = "info",
    output_format: str = "structured",
    *,
    model: Optional[str] = None,
) -> str:
    """
    Execute a comprehensive code review with structured output.

    Args:
        code: Code to review
        language: Programming language
        focus_areas: Comma-separated focus areas
        severity_threshold: Minimum severity to report
        output_format: Output format

    Returns:
        JSON string with code review results
    """
    if len(code) > GEMINI_CODE_REVIEW_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Code exceeds limit of {GEMINI_CODE_REVIEW_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    try:
        from prompts.code_review_template import get_structured_code_review_prompt
        prompt = get_structured_code_review_prompt(
            code=code,
            language=language,
            focus_areas=focus_areas,
            severity_threshold=severity_threshold,
            output_format=output_format
        )
    except ImportError:
        # Fallback prompt
        focus_text = f"\n\nFocus areas: {focus_areas}" if focus_areas else ""
        lang_text = f"\n\nLanguage: {language}" if language else ""
        prompt = f"""IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Only provide your written review.

Perform a comprehensive code review.{focus_text}{lang_text}

Code:
{code}

Provide analysis in {output_format} format with severity levels."""

    effective_model = get_task_model("code_review", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args, mutating=False, timeout=get_task_timeout("code_review"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


async def execute_extract_structured(
    content: str,
    schema: str,
    examples: Optional[str] = None,
    strict_mode: bool = True,
    model: Optional[str] = None
) -> str:
    """
    Extract structured data using JSON schemas.

    Args:
        content: Content to analyze
        schema: JSON schema for output (must be a JSON object)
        examples: Optional examples
        strict_mode: True enforces the schema upstream via agy's --json-schema
                     (agy >= 1.1.8), returning a validated `structured_output`.
                     False uses prompt-only extraction.
        model: Model to use. Resolved via get_task_model().

    Returns:
        JSON string with extracted data. On the enforced path the response
        carries `structured_output` (already schema-validated by agy) plus
        `schema_validation: "enforced"`; otherwise `schema_validation` is
        "prompt_only".
    """
    total_length = len(content) + len(schema) + len(examples or "")
    if total_length > GEMINI_EXTRACT_STRUCTURED_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Input exceeds limit of {GEMINI_EXTRACT_STRUCTURED_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    # Validate schema is valid JSON
    try:
        parsed_schema = json.loads(schema)
    except json.JSONDecodeError as e:
        return json.dumps({
            "status": "error",
            "error": f"Invalid JSON schema: {str(e)}",
            "error_code": "INVALID_SCHEMA"
        })

    # agy's --json-schema expects a schema object. A bare scalar or array parses
    # as JSON but is not a usable schema, and passing it through would fail
    # inside agy with a less specific message.
    if not isinstance(parsed_schema, dict):
        return json.dumps({
            "status": "error",
            "error": (
                f"Schema must be a JSON object, got "
                f"{type(parsed_schema).__name__}."
            ),
            "error_code": "INVALID_SCHEMA"
        })

    try:
        from prompts.extract_structured_template import get_extract_structured_prompt
        prompt = get_extract_structured_prompt(
            content=content,
            schema=schema,
            examples=examples,
            strict_mode=strict_mode
        )
    except ImportError:
        strict_text = " Strictly follow the schema." if strict_mode else ""
        example_text = f"\n\nExamples:\n{examples}" if examples else ""
        prompt = f"""IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Only return the extracted data.

Extract structured data from the following content according to this schema.{strict_text}

Schema:
{schema}{example_text}

Content:
{content}

Return valid JSON matching the schema."""

    effective_model = get_task_model("extract_structured", model)

    # Upstream enforcement when available: agy validates the model's output
    # against the schema and returns it as a parsed `structured_output` object,
    # instead of us asking nicely in the prompt and hoping.
    from modules.utils.cli_utils import (
        resolve_output_format, _get_cached_or_sync_version, _parse_version,
    )
    cached_version = _get_cached_or_sync_version()
    version = _parse_version(cached_version) if cached_version else (0, 0, 0)
    try:
        transport = resolve_output_format(version, bool(cached_version))
    except CLIExecutionError as e:
        # CLI_OUTPUT_FORMAT=json on an agy that cannot produce it. Return the
        # documented error shape rather than raising out of the tool.
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "CONFIG_ERROR",
        })
    # transport == "json" already implies version >= 1.1.8, which is also the
    # --json-schema floor, so no separate version check is needed here.
    enforce = strict_mode and transport == "json"

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(
        prompt=cleaned_prompt,
        files=files,
        model=effective_model,
        json_schema=schema if enforce else None,
    )

    try:
        result = await execute_cli_with_retry(args, mutating=False, timeout=get_task_timeout("extract_structured"))
        result = add_model_metadata(result, await validate_model(effective_model))
        result["schema_validation"] = "enforced" if enforce else "prompt_only"
        if enforce and "structured_output" not in result:
            result["warning"] = " | ".join(filter(None, [
                result.get("warning"),
                "agy did not return a structured_output object despite "
                "--json-schema; treat the text response as unvalidated.",
            ]))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": type(e).__name__.replace("CLI", "").replace("Error", "").upper(),
            "schema_validation": "enforced" if enforce else "prompt_only",
        })


async def execute_git_diff_review(
    diff: str,
    context_lines: int = 3,
    review_type: str = "comprehensive",
    base_branch: Optional[str] = None,
    commit_message: Optional[str] = None,
    *,
    model: Optional[str] = None,
) -> str:
    """
    Analyze git diffs with contextual feedback.

    Args:
        diff: Git diff content
        context_lines: Number of context lines
        review_type: Type of review
        base_branch: Base branch
        commit_message: Commit message

    Returns:
        JSON string with diff analysis
    """
    if len(diff) > GEMINI_GIT_DIFF_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Diff exceeds limit of {GEMINI_GIT_DIFF_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    try:
        from prompts.git_diff_review_template import get_git_diff_review_prompt
        prompt = get_git_diff_review_prompt(
            diff=diff,
            context_lines=context_lines,
            review_type=review_type,
            base_branch=base_branch,
            commit_message=commit_message
        )
    except ImportError:
        branch_text = f"\n\nBase branch: {base_branch}" if base_branch else ""
        commit_text = f"\n\nCommit message: {commit_message}" if commit_message else ""
        prompt = f"""Review the following git diff ({review_type} review).{branch_text}{commit_text}

Diff:
{diff}

Provide feedback on:
1. Code quality changes
2. Potential issues introduced
3. Security implications
4. Suggestions for improvement"""

    effective_model = get_task_model("git_diff_review", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args, mutating=False, timeout=get_task_timeout("git_diff_review"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })
