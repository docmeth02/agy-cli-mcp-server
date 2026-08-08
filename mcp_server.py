"""
Antigravity CLI MCP Server

A production-ready Model Context Protocol (MCP) server that bridges Google's
Antigravity CLI (agy) with MCP-compatible clients like Claude Code and Claude Desktop.

This server provides 27 specialized tools for seamless AI workflows.
"""
import sys
from pathlib import Path

# Ensure imports work regardless of working directory
script_dir = Path(__file__).parent.resolve()
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

import os
import json
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Configure logging
log_level = os.getenv("GEMINI_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP("gemini-cli-mcp-server")

# Import utilities
from modules.utils.cli_utils import (
    execute_cli_with_retry,
    extract_file_refs,
    _build_cli_args,
    get_cli_help,
    get_cli_version,
    get_available_models,
    get_available_agents,
    model_selection_value,
    model_accepted_values,
    run_readonly_slash_command,
    validate_model,
    validate_agent,
    add_model_metadata,
    get_metrics,
    validate_cli_setup,
    CLIExecutionError,
    CLITimeoutError,
    CLIRateLimitError,
)


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 'Z' string, for stamping cached reads."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# PHASE 1: Core CLI Tools (gemini_cli, gemini_help, gemini_version)
# ============================================================================

@mcp.tool()
async def gemini_cli(command: str) -> str:
    """
    Execute any Antigravity CLI command directly with comprehensive error handling.

    Args:
        command: The Antigravity CLI command to execute (without the 'agy' prefix)

    Returns:
        JSON string with status, stdout, stderr, and return_code

    Examples:
        gemini_cli(command="--print 'Hello world'")
        gemini_cli(command="--print 'Explain AI' --add-dir src/")
    """
    if not command or not command.strip():
        return json.dumps({
            "status": "error",
            "error": "Command cannot be empty",
            "error_code": "INVALID_INPUT"
        })

    try:
        import shlex
        args = shlex.split(command)
    except ValueError as e:
        return json.dumps({
            "status": "error",
            "error": f"Invalid command format: {str(e)}",
            "error_code": "INVALID_COMMAND"
        })

    try:
        result = await execute_cli_with_retry(args)
        return json.dumps(result, indent=2)
    except CLITimeoutError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "TIMEOUT"
        })
    except CLIRateLimitError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "RATE_LIMIT"
        })
    except CLIExecutionError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "EXECUTION_ERROR"
        })
    except Exception as e:
        logger.error(f"Unexpected error in gemini_cli: {e}")
        return json.dumps({
            "status": "error",
            "error": "An unexpected error occurred",
            "error_code": "INTERNAL_ERROR"
        })


@mcp.tool()
async def gemini_help() -> str:
    """
    Get cached Antigravity CLI help information (30-minute TTL).

    Returns:
        Antigravity CLI help text

    Examples:
        gemini_help()
    """
    try:
        help_text = await get_cli_help()
        return help_text
    except CLIExecutionError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "EXECUTION_ERROR"
        })
    except Exception as e:
        logger.error(f"Unexpected error in gemini_help: {e}")
        return json.dumps({
            "status": "error",
            "error": "Failed to get help",
            "error_code": "INTERNAL_ERROR"
        })


@mcp.tool()
async def gemini_version() -> str:
    """
    Get cached Antigravity CLI version information (30-minute TTL).

    Returns:
        Antigravity CLI version information

    Examples:
        gemini_version()
    """
    try:
        version_text = await get_cli_version()
        return version_text
    except CLIExecutionError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "EXECUTION_ERROR"
        })
    except Exception as e:
        logger.error(f"Unexpected error in gemini_version: {e}")
        return json.dumps({
            "status": "error",
            "error": "Failed to get version",
            "error_code": "INTERNAL_ERROR"
        })


# ============================================================================
# PHASE 2: Core Tools (gemini_prompt, gemini_models, gemini_metrics)
# ============================================================================

# Import configuration when available
try:
    from modules.config.cli_config import (
        GEMINI_PROMPT_LIMIT,
        GEMINI_SANDBOX_LIMIT,
        GEMINI_SUMMARIZE_LIMIT,
        GEMINI_SUMMARIZE_FILES_LIMIT,
        GEMINI_EVAL_LIMIT,
        GEMINI_REVIEW_LIMIT,
        GEMINI_VERIFY_LIMIT,
        GEMINI_COLLABORATION_LIMIT,
        get_task_model,
        get_task_timeout,
    )
except ImportError:
    GEMINI_PROMPT_LIMIT = 100000
    GEMINI_SANDBOX_LIMIT = 200000
    GEMINI_SUMMARIZE_LIMIT = 400000
    GEMINI_SUMMARIZE_FILES_LIMIT = 800000
    GEMINI_EVAL_LIMIT = 500000
    GEMINI_REVIEW_LIMIT = 300000
    GEMINI_VERIFY_LIMIT = 800000
    GEMINI_COLLABORATION_LIMIT = 500000

    def get_task_model(task: str, explicit: Optional[str] = None) -> Optional[str]:
        return explicit or None

    def get_task_timeout(task: str, explicit: Optional[int] = None) -> int:
        return explicit or 300


@mcp.tool()
async def gemini_prompt(
    prompt: str,
    model: Optional[str] = None,
    agent: Optional[str] = None,
    sandbox: bool = False,
    debug: bool = False,
    readonly: bool = False,
    project: Optional[str] = None,
    interpret_slash_commands: bool = False,
) -> str:
    """
    Send prompts to Antigravity CLI for execution (100,000 char limit).

    Use this tool when you want the AI to IMPLEMENT something — write code,
    create files, or execute commands. For analysis, brainstorming, opinions,
    or plan evaluation, prefer gemini_eval_plan (read-only by design) or set
    readonly=True to prevent file modifications.

    Args:
        prompt: The prompt to send to Antigravity CLI
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high"
               or "Gemini 3.1 Pro (High)"). Defaults to agy's default.
               See gemini_models() for the full list.
        agent: Custom agent to use (agy >= 1.1.1). See gemini_agents() for
               available agents. Omit to use the default agent.
        sandbox: Whether to run in sandbox mode
        debug: Whether to enable debug output (ignored for agy)
        readonly: When True, instructs the AI to only respond with text and not
                  create, modify, or delete any files. Use for brainstorming,
                  analysis, opinions, and planning tasks.
        project: Project ID for session isolation (agy >= 1.0.12).
        interpret_slash_commands: When False (default) the prompt is sent to the
                  model verbatim, even if it begins with "/". Set True to let
                  agy expand its own slash commands and skills (agy >= 1.1.9),
                  e.g. prompt="/antigravity-guide explain customizations". Note
                  that with this enabled, an interactive-only command such as
                  "/clear" will fail instead of reaching the model. Has no
                  effect when readonly=True, which prepends a preamble so the
                  slash command is no longer at the start of the prompt.

    Returns:
        JSON string with the response

    Examples:
        gemini_prompt(prompt="Explain quantum computing")
        gemini_prompt(prompt="Analyze @src/auth.py", readonly=True)
        gemini_prompt(prompt="Complex analysis", model="gemini-3.1-pro-high")
    """
    if readonly:
        prompt = (
            "IMPORTANT: This is a read-only request. Do NOT create, modify, or "
            "delete any files. Do NOT execute any code or run any commands. "
            "Only provide your written response.\n\n" + prompt
        )

    if len(prompt) > GEMINI_PROMPT_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Prompt exceeds limit of {GEMINI_PROMPT_LIMIT:,} characters "
                     f"(got {len(prompt):,})",
            "error_code": "INPUT_TOO_LARGE"
        })

    effective_model = get_task_model("prompt", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(
        prompt=cleaned_prompt,
        sandbox=sandbox,
        debug=debug,
        files=files,
        model=effective_model,
        agent=agent,
        project=project,
        interpret_slash_commands=interpret_slash_commands,
    )

    try:
        result = await execute_cli_with_retry(args)
        result = add_model_metadata(result, await validate_model(effective_model))
        result = add_model_metadata(result, await validate_agent(agent))
        return json.dumps(result, indent=2)
    except CLITimeoutError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "TIMEOUT"
        })
    except CLIRateLimitError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "RATE_LIMIT"
        })
    except CLIExecutionError as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "EXECUTION_ERROR"
        })


@mcp.tool()
async def gemini_models() -> str:
    """
    List all available AI models with selection guidance.

    Returns:
        JSON with available models and per-tool defaults.

    Examples:
        gemini_models()
    """
    models = await get_available_models()

    if not models:
        return json.dumps({
            "status": "success",
            "models": [],
            "note": "Could not fetch models list. Try a slug like "
                    "'gemini-3.1-pro-high' or a display name like "
                    "'Gemini 3.1 Pro (High)'. Run `agy models` for the live list."
        }, indent=2)

    categorized = []
    for record in models:
        display = record.get("display_name") or ""
        categorized.append({
            # The exact string to pass as the `model` parameter. Named "model"
            # rather than "name" so a caller copying a field verbatim copies a
            # value agy actually accepts, and copies the stable slug over the
            # release-dependent display name.
            "model": model_selection_value(record),
            "display_name": display,
            "accepted_values": model_accepted_values(record),
            "category": "primary" if "gemini" in display.lower() else "alternative",
        })

    from modules.config.cli_config import TASK_MODEL_DEFAULTS
    return json.dumps({
        "status": "success",
        "models": categorized,
        "guidance": (
            "Pass the exact `model` value from the list above. Both the slug "
            "(e.g. 'gemini-3.1-pro-high') and the display name (e.g. "
            "'Gemini 3.1 Pro (High)') are accepted, but slugs are stable across "
            "agy releases and are preferred. Short names (pro/flash/claude) were "
            "dropped in agy 1.1.4 and will hard-fail. Pro tiers are High and Low "
            "only; Flash offers High, Medium and Low."
        ),
        "task_defaults": {
            k: v or "(agy default)"
            for k, v in TASK_MODEL_DEFAULTS.items()
        },
    }, indent=2)


@mcp.tool()
async def gemini_agents() -> str:
    """
    List available custom agents.

    Agents are specialized personas that can be selected via the `agent`
    parameter on gemini_prompt and gemini_sandbox (requires agy >= 1.1.1).

    Returns:
        JSON with available agent names.

    Examples:
        gemini_agents()
    """
    agents = await get_available_agents()

    return json.dumps({
        "status": "success",
        "agents": agents,
        "note": (
            "Pass an agent name as the `agent` parameter to gemini_prompt "
            "or gemini_sandbox to use it. Requires agy >= 1.1.1."
            if agents
            else "No custom agents found. Create agents via agy's "
                 "/agents panel or in ~/.gemini/config/agents/."
        ),
    }, indent=2)


@mcp.tool()
async def gemini_metrics() -> str:
    """
    Get comprehensive server performance metrics and statistics.

    Returns:
        JSON with server metrics including execution stats, cache stats, etc.

    Examples:
        gemini_metrics()
    """
    try:
        metrics = get_metrics()

        # Add cache-specific stats
        from modules.utils.cli_utils import HELP_CACHE, VERSION_CACHE

        cache_stats = {
            "help_cache": {
                "size": len(HELP_CACHE),
                "maxsize": HELP_CACHE.maxsize,
                "ttl": HELP_CACHE.ttl
            },
            "version_cache": {
                "size": len(VERSION_CACHE),
                "maxsize": VERSION_CACHE.maxsize,
                "ttl": VERSION_CACHE.ttl
            },
        }

        try:
            from security.security_monitor import get_security_monitor
            security_stats = get_security_monitor().get_stats()
        except Exception:
            security_stats = {}

        return json.dumps({
            "status": "success",
            "metrics": metrics,
            "cache_stats": cache_stats,
            "security_stats": security_stats,
            "server_info": {
                "name": "gemini-cli-mcp-server",
                "tools_available": 27,
                "python_version": os.sys.version
            }
        }, indent=2)
    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": "METRICS_ERROR"
        })


# ============================================================================
# PHASE 3: System Tools
# ============================================================================

@mcp.tool()
async def gemini_sandbox(
    prompt: str,
    model: Optional[str] = None,
    agent: Optional[str] = None,
    project: Optional[str] = None,
    interpret_slash_commands: bool = False,
) -> str:
    """
    Execute prompts in sandbox mode for code execution (200,000 char limit).

    Note: --sandbox restricts terminal commands, NOT the filesystem. This tool
    can still create or modify files anywhere. Do not treat it as a jail.

    Args:
        prompt: The prompt to execute in sandbox mode
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to agy's default. See gemini_models().
        agent: Custom agent to use (agy >= 1.1.1). See gemini_agents().
        project: Project ID for session isolation (agy >= 1.0.12).
        interpret_slash_commands: When False (default) the prompt is sent
                  verbatim, even if it begins with "/". Set True to let agy
                  expand its own slash commands and skills (agy >= 1.1.9).

    Returns:
        JSON string with execution results

    Examples:
        gemini_sandbox(prompt="Write and run a Python script to analyze data")
        gemini_sandbox(prompt="Test this code in sandbox mode")
    """
    if len(prompt) > GEMINI_SANDBOX_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Prompt exceeds limit of {GEMINI_SANDBOX_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    effective_model = get_task_model("sandbox", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(
        prompt=cleaned_prompt,
        sandbox=True,
        files=files,
        model=effective_model,
        agent=agent,
        project=project,
        interpret_slash_commands=interpret_slash_commands,
    )

    try:
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("sandbox"))
        result = add_model_metadata(result, await validate_model(effective_model))
        result = add_model_metadata(result, await validate_agent(agent))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "error_code": type(e).__name__.replace("CLI", "").replace("Error", "").upper()
        })


@mcp.tool()
async def gemini_cache_stats() -> str:
    """
    Get cache statistics for all cache backends.

    Returns:
        JSON with cache statistics for all caches

    Examples:
        gemini_cache_stats()
    """
    from modules.utils.cli_utils import HELP_CACHE, VERSION_CACHE

    stats = {
        "help_cache": {
            "size": len(HELP_CACHE),
            "maxsize": HELP_CACHE.maxsize,
            "ttl_seconds": HELP_CACHE.ttl,
            "items": list(HELP_CACHE.keys())
        },
        "version_cache": {
            "size": len(VERSION_CACHE),
            "maxsize": VERSION_CACHE.maxsize,
            "ttl_seconds": VERSION_CACHE.ttl,
            "items": list(VERSION_CACHE.keys())
        },
    }

    return json.dumps({
        "status": "success",
        "cache_statistics": stats
    }, indent=2)


@mcp.tool()
async def gemini_rate_limiting_stats() -> str:
    """
    Get comprehensive rate limiting and quota statistics.

    Returns:
        JSON with rate limiting statistics

    Examples:
        gemini_rate_limiting_stats()
    """
    metrics = get_metrics()

    rate_stats = {
        "rate_limit_hits": metrics.get("rate_limit_hits", 0),
        "fallback_count": metrics.get("fallback_count", 0),
        "commands_executed": metrics.get("commands_executed", 0),
        "success_rate": metrics.get("success_rate", 0),
    }

    return json.dumps({
        "status": "success",
        "rate_limiting_statistics": rate_stats
    }, indent=2)


@mcp.tool()
async def gemini_usage() -> str:
    """
    Show remaining Gemini/Claude/GPT model quota for the signed-in account.

    Reports the weekly and 5-hour limit remaining for each model group. Free to
    call: agy answers this without starting an agent turn, so it consumes no
    quota and creates no conversation. Requires agy >= 1.1.11.

    Returns:
        JSON with per-group quota buckets (window, remaining percent, reset time).

    Examples:
        gemini_usage()
    """
    from modules.utils.cli_utils import USAGE_CACHE

    if "usage" in USAGE_CACHE:
        return USAGE_CACHE["usage"]

    try:
        result = await run_readonly_slash_command("/usage")
    except CLIExecutionError as e:
        return json.dumps({
            "status": "error", "error": str(e), "error_code": "USAGE_FAILED",
        })

    if result["status"] != "success":
        return json.dumps({
            "status": "error",
            "error": result.get("error", "Failed to read usage"),
            "error_code": "USAGE_FAILED",
            "note": "Requires agy >= 1.1.11 for print-mode /usage support.",
        })

    groups = []
    data = result.get("data") or {}
    for group in (data.get("groups") or []):
        groups.append({
            "group": group.get("name"),
            "models": group.get("description"),
            "buckets": [
                {
                    "window": b.get("window"),
                    "remaining_percent": (
                        round(b["remaining_fraction"] * 100, 2)
                        if isinstance(b.get("remaining_fraction"), (int, float))
                        else None
                    ),
                    "resets_at": b.get("reset_time"),
                    "detail": b.get("description"),
                }
                for b in (group.get("buckets") or [])
            ],
        })

    payload = {"status": "success", "groups": groups}
    if not groups:
        # agy < 1.1.8 has no structured payload; hand back the text records.
        payload["records"] = result.get("records", [])
    # Cached for 60s, so stamp the reading: an unchanged percentage after a heavy
    # run is otherwise indistinguishable from a stale cache hit.
    payload["retrieved_at"] = _utc_now_iso()
    payload["cache_ttl_seconds"] = USAGE_CACHE.ttl
    payload["notes"] = (
        "Quota is consumed proportionally to token cost. This read is free and "
        "does not itself consume quota."
    )

    encoded = json.dumps(payload, indent=2)
    USAGE_CACHE["usage"] = encoded
    return encoded


@mcp.tool()
async def gemini_credits() -> str:
    """
    Show the remaining paid G1 credit balance for the signed-in account.

    Free to call: agy answers this without starting an agent turn. Requires
    agy >= 1.1.11.

    Note: agy inherits the `use_ai_credits` setting from ~/.gemini/settings.json,
    so once the standard quota is exhausted a headless run can spend paid credits
    with no CLI flag to prevent it. Check gemini_usage() alongside this to see
    whether quota is close to exhaustion. This tool only reports; it does not
    gate any other tool.

    Returns:
        JSON with remaining_credits and an upgrade URI when applicable.

    Examples:
        gemini_credits()
    """
    from modules.utils.cli_utils import CREDITS_CACHE

    if "credits" in CREDITS_CACHE:
        return CREDITS_CACHE["credits"]

    try:
        result = await run_readonly_slash_command("/credits")
    except CLIExecutionError as e:
        return json.dumps({
            "status": "error", "error": str(e), "error_code": "CREDITS_FAILED",
        })

    if result["status"] != "success":
        return json.dumps({
            "status": "error",
            "error": result.get("error", "Failed to read credits"),
            "error_code": "CREDITS_FAILED",
            "note": "Requires agy >= 1.1.11 for print-mode /credits support.",
        })

    data = result.get("data") or {}
    payload = {"status": "success"}
    if "remaining_credits" in data:
        payload["remaining_credits"] = data["remaining_credits"]
        if data.get("upgrade_uri"):
            payload["upgrade_uri"] = data["upgrade_uri"]
    else:
        # agy < 1.1.8: no structured payload, return the text records.
        payload["records"] = result.get("records", [])
    payload["retrieved_at"] = _utc_now_iso()
    payload["cache_ttl_seconds"] = CREDITS_CACHE.ttl
    payload["notes"] = (
        "This read is free and does not consume quota. Reported for visibility "
        "only: no tool is gated on this balance."
    )

    encoded = json.dumps(payload, indent=2)
    CREDITS_CACHE["credits"] = encoded
    return encoded


# ============================================================================
# PHASE 4: Analysis Tools
# ============================================================================

@mcp.tool()
async def gemini_summarize(
    content: str,
    focus: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    Summarize content with focus-specific analysis (400,000 char limit).

    Args:
        content: Content to summarize (supports @filename syntax)
        focus: Optional focus area (e.g., "architecture and design patterns")
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to agy's default.

    Returns:
        JSON string with summarization results

    Examples:
        gemini_summarize(content="@src/ @tests/", focus="architecture")
    """
    if len(content) > GEMINI_SUMMARIZE_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Content exceeds limit of {GEMINI_SUMMARIZE_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    # Build prompt using template
    try:
        from prompts.summarize_template import get_summarize_prompt
        prompt = get_summarize_prompt(content, focus)
    except ImportError:
        # Fallback prompt
        focus_text = f" Focus on: {focus}" if focus else ""
        prompt = f"IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Only provide your written summary.\n\nPlease summarize the following content.{focus_text}\n\n{content}"

    effective_model = get_task_model("summarize", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args)
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


@mcp.tool()
async def gemini_summarize_files(
    files: str,
    focus: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    File-based summarization optimized for @filename syntax (800,000 char limit).

    Args:
        files: Files to summarize using @filename syntax (e.g., "@src/ @docs/")
        focus: Optional focus area for analysis
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to agy's default.

    Returns:
        JSON string with summarization results

    Examples:
        gemini_summarize_files(files="@src/ @docs/ @tests/", focus="complete system analysis")
    """
    if len(files) > GEMINI_SUMMARIZE_FILES_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Files specification exceeds limit of {GEMINI_SUMMARIZE_FILES_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    focus_text = f" Focus on: {focus}" if focus else ""
    prompt = f"IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Only provide your written summary.\n\nAnalyze and summarize the following files.{focus_text}\n\n{files}"

    effective_model = get_task_model("summarize_files", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("summarize_files"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


@mcp.tool()
async def gemini_eval_plan(
    plan: str,
    context: Optional[str] = None,
    requirements: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    Get a second opinion on implementation plans, architecture decisions,
    brainstorming ideas, or technical proposals (500,000 char limit). This tool
    is READ-ONLY — it will never create, modify, or delete files.

    Use this instead of gemini_prompt when you want analysis, feedback, or
    discussion without any risk of the AI modifying the codebase.

    Args:
        plan: The plan, idea, or proposal to evaluate
        context: Optional context (e.g., "Node.js REST API with MongoDB")
        requirements: Optional requirements or constraints
        model: Model to use (slug or display name, e.g. "gemini-3.6-flash-medium").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON string with evaluation results

    Examples:
        gemini_eval_plan(plan="1. Create JWT auth...", context="Express.js API")
        gemini_eval_plan(plan="Should we use microservices or monolith?", context="Early stage startup")
    """
    total_length = len(plan) + len(context or "") + len(requirements or "")
    if total_length > GEMINI_EVAL_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Input exceeds limit of {GEMINI_EVAL_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    try:
        from prompts.eval_template import get_eval_plan_prompt
        prompt = get_eval_plan_prompt(plan, context, requirements)
    except ImportError:
        # Fallback prompt
        context_text = f"\n\nContext: {context}" if context else ""
        req_text = f"\n\nRequirements: {requirements}" if requirements else ""
        prompt = f"""IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Do NOT implement anything. Only provide your written evaluation.

Please evaluate the following implementation plan for completeness,
correctness, and potential issues.{context_text}{req_text}

Plan:
{plan}

Provide a detailed analysis with:
1. Strengths
2. Potential issues
3. Missing considerations
4. Recommendations"""

    effective_model = get_task_model("eval_plan", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("eval_plan"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


@mcp.tool()
async def gemini_review_code(
    code: str,
    purpose: Optional[str] = None,
    context: Optional[str] = None,
    language: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    Review specific code suggestions with detailed analysis (300,000 char limit).

    Args:
        code: Code to review (supports @filename syntax)
        purpose: Purpose of the review (e.g., "Security review")
        context: Additional context
        language: Programming language
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON string with review results

    Examples:
        gemini_review_code(code="@src/auth.py", purpose="Security review", language="python")
    """
    total_length = len(code) + len(purpose or "") + len(context or "")
    if total_length > GEMINI_REVIEW_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Input exceeds limit of {GEMINI_REVIEW_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    try:
        from prompts.review_template import get_review_code_prompt
        prompt = get_review_code_prompt(code, purpose, context, language)
    except ImportError:
        # Fallback prompt
        purpose_text = f"\n\nPurpose: {purpose}" if purpose else ""
        context_text = f"\n\nContext: {context}" if context else ""
        lang_text = f"\n\nLanguage: {language}" if language else ""
        prompt = f"""IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Only provide your written review.

Please review the following code.{purpose_text}{context_text}{lang_text}

Code:
{code}

Provide a detailed review covering:
1. Code quality
2. Potential bugs
3. Security concerns
4. Performance considerations
5. Recommendations"""

    effective_model = get_task_model("review_code", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("review_code"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


@mcp.tool()
async def gemini_verify_solution(
    solution: str,
    requirements: Optional[str] = None,
    test_criteria: Optional[str] = None,
    context: Optional[str] = None,
    model: Optional[str] = None
) -> str:
    """
    Comprehensive verification of complete solutions (800,000 char limit).

    Args:
        solution: Complete solution to verify
        requirements: Original requirements
        test_criteria: Testing and performance criteria
        context: Deployment context
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON string with verification results

    Examples:
        gemini_verify_solution(solution="...", requirements="Auth system", test_criteria="99.9% uptime")
    """
    total_length = (len(solution) + len(requirements or "") +
                    len(test_criteria or "") + len(context or ""))
    if total_length > GEMINI_VERIFY_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Input exceeds limit of {GEMINI_VERIFY_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    try:
        from prompts.verify_template import get_verify_solution_prompt
        prompt = get_verify_solution_prompt(solution, requirements, test_criteria, context)
    except ImportError:
        # Fallback prompt
        req_text = f"\n\nRequirements: {requirements}" if requirements else ""
        test_text = f"\n\nTest Criteria: {test_criteria}" if test_criteria else ""
        ctx_text = f"\n\nContext: {context}" if context else ""
        prompt = f"""IMPORTANT: This is an analysis-only task. Do NOT create, modify, or delete any files. Do NOT execute any code. Only provide your written verification.

Please verify the following solution comprehensively.{req_text}{test_text}{ctx_text}

Solution:
{solution}

Verify:
1. Completeness against requirements
2. Code correctness
3. Security considerations
4. Performance implications
5. Test coverage adequacy
6. Production readiness"""

    effective_model = get_task_model("verify_solution", model)

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)

    try:
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("verify_solution"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        })


# ============================================================================
# PHASE 5: Conversation Management
# ============================================================================

@mcp.tool()
async def gemini_start_conversation(
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[str] = None,
    expiration_hours: int = 24
) -> str:
    """
    Register conversation metadata (title, tags, expiration) in the local sidecar.

    IMPORTANT — this does NOT create a conversation inside agy. It only records
    metadata against a freshly minted id, and agy never learns about that id.
    Because agy silently ignores an unknown --conversation id (it starts a new
    context under a different id and reports success), the id returned here is
    NOT yet usable with gemini_continue_conversation, which will refuse it with
    CONVERSATION_NOT_BOUND rather than silently discard your history.

    To hold a real multi-turn conversation today:
      1. call gemini_prompt(...) for the first turn, then
      2. call gemini_list_conversations() and take a conversation_id whose
         has_native_file is true, then
      3. pass that id to gemini_continue_conversation().

    Args:
        title: Optional title for the conversation
        description: Optional description
        tags: Optional comma-separated tags
        expiration_hours: Hours until conversation expires (default: 24)

    Returns:
        JSON with conversation_id and details. The id is metadata-only until an
        agy-side conversation file exists for it.

    Examples:
        gemini_start_conversation(title="Python Help", tags="python,development")
    """
    try:
        from modules.services.conversation_manager import ConversationManager
        manager = ConversationManager()
        conversation = await manager.create_conversation(
            title=title,
            description=description,
            tags=tags.split(",") if tags else None,
            expiration_hours=expiration_hours
        )
        return json.dumps({
            "status": "success",
            "conversation": conversation
        }, indent=2)
    except ImportError:
        logger.error("Failed to import ConversationManager", exc_info=True)
        import uuid
        conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        return json.dumps({
            "status": "success",
            "conversation": {
                "conversation_id": conversation_id,
                "title": title,
                "description": description,
                "tags": tags.split(",") if tags else [],
                "created_at": __import__("time").time(),
                "expiration_hours": expiration_hours,
                "message_count": 0
            }
        }, indent=2)


@mcp.tool()
async def gemini_continue_conversation(
    conversation_id: str,
    prompt: str,
    model: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    """
    Continue an existing agy conversation with its context history.

    The conversation_id must be one agy actually knows about — take it from
    gemini_list_conversations() where has_native_file is true. An id from
    gemini_start_conversation() is metadata-only and is rejected with
    CONVERSATION_NOT_BOUND, because agy silently ignores an unknown
    --conversation id and would start a fresh, historyless context instead.

    Args:
        conversation_id: ID of an existing agy conversation (a UUID). Must have
               has_native_file true in gemini_list_conversations().
        prompt: The new prompt/message
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to agy's default; the model is not carried over from
               earlier turns of the conversation.
        project: Project ID for session isolation (agy >= 1.0.12).

    Returns:
        JSON with response and updated conversation state

    Examples:
        gemini_continue_conversation(
            conversation_id="ea160180-a361-4cd8-808f-3252614f45cd",
            prompt="How do I optimize this?",
        )
    """
    effective_model = get_task_model("continue_conversation", model)

    try:
        from modules.services.conversation_manager import ConversationManager
        manager = ConversationManager()
        result = await manager.continue_conversation(
            conversation_id=conversation_id,
            prompt=prompt,
            model=effective_model,
            project=project,
        )
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)
    except ImportError:
        logger.error("Failed to import ConversationManager", exc_info=True)
        cleaned_prompt, files = extract_file_refs(prompt)
        args = _build_cli_args(
            prompt=cleaned_prompt, files=files, model=effective_model,
            project=project,
        )
        result = await execute_cli_with_retry(args)
        return json.dumps(add_model_metadata({
            "status": "success",
            "conversation_id": conversation_id,
            "response": result
        }, await validate_model(effective_model)), indent=2)


@mcp.tool()
async def gemini_list_conversations(
    limit: int = 20,
    status_filter: Optional[str] = None
) -> str:
    """
    List active conversations with metadata.

    Args:
        limit: Maximum number of conversations to return
        status_filter: Optional filter (active, expired)

    Returns:
        JSON with list of conversations

    Examples:
        gemini_list_conversations(limit=10, status_filter="active")
    """
    try:
        from modules.services.conversation_manager import ConversationManager
        manager = ConversationManager()
        conversations = manager.list_conversations(
            limit=limit,
            status_filter=status_filter
        )
        return json.dumps({
            "status": "success",
            "conversations": conversations,
            "total": len(conversations)
        }, indent=2)
    except ImportError:
        logger.error("Failed to import ConversationManager", exc_info=True)
        return json.dumps({
            "status": "success",
            "conversations": [],
            "total": 0,
            "note": "Conversation management not fully configured"
        }, indent=2)


@mcp.tool()
async def gemini_clear_conversation(conversation_id: str) -> str:
    """
    Clear/delete a specific conversation.

    Args:
        conversation_id: ID of the conversation to clear

    Returns:
        JSON with confirmation

    Examples:
        gemini_clear_conversation(conversation_id="conv_12345")
    """
    try:
        from modules.services.conversation_manager import ConversationManager
        manager = ConversationManager()
        result = await manager.clear_conversation(conversation_id)
        return json.dumps(result, indent=2)
    except ImportError:
        logger.error("Failed to import ConversationManager", exc_info=True)
        return json.dumps({
            "status": "success",
            "message": f"Conversation {conversation_id} cleared",
            "note": "Conversation management not fully configured"
        }, indent=2)


@mcp.tool()
async def gemini_conversation_stats() -> str:
    """
    Get conversation system statistics and health.

    Returns:
        JSON with conversation system statistics

    Examples:
        gemini_conversation_stats()
    """
    try:
        from modules.services.conversation_manager import ConversationManager
        manager = ConversationManager()
        stats = manager.get_stats()
        return json.dumps({
            "status": "success",
            "statistics": stats
        }, indent=2)
    except ImportError:
        logger.error("Failed to import ConversationManager", exc_info=True)
        return json.dumps({
            "status": "success",
            "statistics": {
                "active_conversations": 0,
                "total_messages": 0,
                "storage_backend": "not_configured"
            }
        }, indent=2)


# ============================================================================
# PHASE 6: Specialized Code Review Tools
# ============================================================================

@mcp.tool()
async def gemini_code_review(
    code: str,
    language: Optional[str] = None,
    focus_areas: Optional[str] = None,
    severity_threshold: str = "info",
    output_format: str = "structured",
    model: Optional[str] = None,
) -> str:
    """
    Comprehensive code analysis with structured output (300,000 char limit).

    Args:
        code: Code to review
        language: Programming language (auto-detected if not specified)
        focus_areas: Comma-separated focus areas (security,performance,quality,best_practices)
        severity_threshold: Minimum severity to report (info, warning, error, critical)
        output_format: Output format (structured, markdown, json)
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON with structured code review

    Examples:
        gemini_code_review(code="@src/api/", focus_areas="security,performance")
    """
    try:
        from modules.core.mcp_code_review_tools import execute_code_review
        return await execute_code_review(
            code=code,
            language=language,
            focus_areas=focus_areas,
            severity_threshold=severity_threshold,
            output_format=output_format,
            model=model,
        )
    except ImportError:
        effective_model = get_task_model("code_review", model)
        focus_text = f"\n\nFocus areas: {focus_areas}" if focus_areas else ""
        lang_text = f"\n\nLanguage: {language}" if language else ""
        prompt = f"""Perform a comprehensive code review.{focus_text}{lang_text}

Code:
{code}

Provide analysis in {output_format} format with severity levels."""

        cleaned_prompt, files = extract_file_refs(prompt)
        args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("code_review"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)


@mcp.tool()
async def gemini_extract_structured(
    content: str,
    schema: str,
    examples: Optional[str] = None,
    strict_mode: bool = True,
    model: Optional[str] = None
) -> str:
    """
    Extract structured data using JSON schemas (200,000 char limit).

    Args:
        content: Content to analyze
        schema: JSON schema defining the output structure
        examples: Optional examples of expected output
        strict_mode: Whether to enforce strict schema compliance
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON with extracted structured data

    Examples:
        gemini_extract_structured(content="@src/", schema='{"type":"object",...}')
    """
    try:
        from modules.core.mcp_code_review_tools import execute_extract_structured
        return await execute_extract_structured(
            content=content,
            schema=schema,
            examples=examples,
            strict_mode=strict_mode,
            model=model
        )
    except ImportError:
        effective_model = get_task_model("extract_structured", model)
        strict_text = " Strictly follow the schema." if strict_mode else ""
        example_text = f"\n\nExamples:\n{examples}" if examples else ""
        prompt = f"""Extract structured data from the following content according to this schema.{strict_text}

Schema:
{schema}{example_text}

Content:
{content}

Return valid JSON matching the schema."""

        cleaned_prompt, files = extract_file_refs(prompt)
        args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("extract_structured"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)


@mcp.tool()
async def gemini_git_diff_review(
    diff: str,
    context_lines: int = 3,
    review_type: str = "comprehensive",
    base_branch: Optional[str] = None,
    commit_message: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Analyze git diffs with contextual feedback (150,000 char limit).

    Args:
        diff: Git diff content or patch
        context_lines: Number of context lines around changes
        review_type: Review type (comprehensive, security_only, performance_only, quick)
        base_branch: Base branch for context
        commit_message: Associated commit message
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON with diff analysis

    Examples:
        gemini_git_diff_review(diff="@pull_request.diff", review_type="security_only")
    """
    try:
        from modules.core.mcp_code_review_tools import execute_git_diff_review
        return await execute_git_diff_review(
            diff=diff,
            context_lines=context_lines,
            review_type=review_type,
            base_branch=base_branch,
            commit_message=commit_message,
            model=model,
        )
    except ImportError:
        effective_model = get_task_model("git_diff_review", model)
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

        cleaned_prompt, files = extract_file_refs(prompt)
        args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("git_diff_review"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)


# ============================================================================
# PHASE 7: Content Comparison
# ============================================================================

@mcp.tool()
async def gemini_content_comparison(
    sources: str,
    comparison_type: str = "semantic",
    output_format: str = "structured",
    include_metrics: bool = True,
    focus_areas: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Advanced multi-source content comparison and analysis (400,000 char limit).

    Args:
        sources: JSON array of sources to compare (supports @filename and URLs)
        comparison_type: Type of comparison (semantic, textual, structural, factual, code)
        output_format: Output format (structured, matrix, summary, detailed, json)
        include_metrics: Include similarity scores and metrics
        focus_areas: Comma-separated focus areas
        model: Model to use (slug or display name, e.g. "gemini-3.1-pro-high").
               Defaults to "gemini-3.1-pro-high".

    Returns:
        JSON with comparison results

    Examples:
        gemini_content_comparison(sources='["@README.md", "@docs/README.md"]', comparison_type="semantic")
    """
    try:
        from modules.core.mcp_content_comparison_tools import execute_content_comparison
        return await execute_content_comparison(
            sources=sources,
            comparison_type=comparison_type,
            output_format=output_format,
            include_metrics=include_metrics,
            focus_areas=focus_areas,
            model=model,
        )
    except ImportError:
        effective_model = get_task_model("content_comparison", model)
        focus_text = f"\n\nFocus on: {focus_areas}" if focus_areas else ""
        prompt = f"""Compare the following sources using {comparison_type} comparison.{focus_text}

Sources:
{sources}

Provide a {output_format} comparison{"with similarity metrics" if include_metrics else ""}."""

        cleaned_prompt, files = extract_file_refs(prompt)
        args = _build_cli_args(prompt=cleaned_prompt, files=files, model=effective_model)
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("content_comparison"))
        result = add_model_metadata(result, await validate_model(effective_model))
        return json.dumps(result, indent=2)


# ============================================================================
# PHASE 8: AI Collaboration Engine
# ============================================================================

@mcp.tool()
async def gemini_ai_collaboration(
    collaboration_mode: str,
    content: str,
    models: Optional[str] = None,
    context: Optional[str] = None,
    conversation_id: Optional[str] = None,
    budget_limit: Optional[float] = None,
    # Sequential mode params
    pipeline_stages: Optional[str] = None,
    handoff_criteria: str = "completion_of_stage",
    quality_gates: str = "standard",
    # Debate mode params
    rounds: int = 3,
    debate_style: str = "constructive",
    convergence_criteria: str = "substantial_agreement",
    # Validation mode params
    validation_criteria: Optional[str] = None,
    confidence_threshold: float = 0.7,
    consensus_method: str = "weighted_majority",
    conflict_resolution: str = "detailed_analysis",
    focus: Optional[str] = None
) -> str:
    """
    Enhanced multi-platform AI collaboration (500,000 char limit).

    Args:
        collaboration_mode: Mode (sequential, debate, validation)
        content: Content to analyze
        models: Comma-separated model list using slugs or display names
               (e.g., "gemini-3.1-pro-high,gemini-3.6-flash-medium")
        context: Additional context
        conversation_id: For stateful conversations
        budget_limit: Deprecated (agy does not support cost budgeting)
        pipeline_stages: Stages for sequential mode
        handoff_criteria: Handoff criteria for sequential
        quality_gates: Quality gates for sequential
        rounds: Number of debate rounds
        debate_style: Style for debate mode
        convergence_criteria: When debate converges
        validation_criteria: Criteria for validation mode
        confidence_threshold: Confidence threshold for validation
        consensus_method: How to reach consensus
        conflict_resolution: How to resolve conflicts
        focus: Focus area

    Returns:
        JSON with collaboration results

    Examples:
        gemini_ai_collaboration(collaboration_mode="debate", content="Microservices vs monolith?", rounds=3)
    """
    try:
        from modules.core.mcp_collaboration_engine import execute_collaboration
        return await execute_collaboration(
            collaboration_mode=collaboration_mode,
            content=content,
            models=models,
            context=context,
            conversation_id=conversation_id,
            budget_limit=budget_limit,
            pipeline_stages=pipeline_stages,
            handoff_criteria=handoff_criteria,
            quality_gates=quality_gates,
            rounds=rounds,
            debate_style=debate_style,
            convergence_criteria=convergence_criteria,
            validation_criteria=validation_criteria,
            confidence_threshold=confidence_threshold,
            consensus_method=consensus_method,
            conflict_resolution=conflict_resolution,
            focus=focus
        )
    except ImportError:
        model_list = (models or "gemini-3.6-flash-medium").split(",")
        results = []

        for m in model_list:
            m = m.strip()
            mode_prompt = {
                "sequential": f"Analyze the following content:\n\n{content}",
                "debate": f"Provide your perspective on:\n\n{content}",
                "validation": f"Validate the following:\n\n{content}"
            }.get(collaboration_mode, content)

            cleaned_prompt, files = extract_file_refs(mode_prompt)
            args = _build_cli_args(
                prompt=cleaned_prompt, files=files, model=m or None,
            )
            try:
                result = await execute_cli_with_retry(
                    args, timeout=get_task_timeout("ai_collaboration")
                )
                results.append(add_model_metadata({
                    "model": m,
                    "response": result.get("stdout", ""),
                }, await validate_model(m or None)))
            except Exception as e:
                results.append({"model": m, "error": str(e)})

        return json.dumps({
            "status": "success",
            "collaboration_mode": collaboration_mode,
            "results": results
        }, indent=2)


# ============================================================================
# MCP Resources: Read-only repository access
# ============================================================================

import re as _re
import subprocess as _subprocess


def _resolve_workspace_root() -> Path:
    """Resolve workspace root to the git repository root, falling back to cwd."""
    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    except (OSError, _subprocess.TimeoutExpired):
        pass
    return Path(os.getcwd()).resolve()


_WORKSPACE_ROOT = _resolve_workspace_root()


def _patch_template_matching():
    """Patch ResourceTemplate.matches to allow multi-segment paths (slashes in {param}).

    The default implementation uses [^/]+ which rejects nested paths like
    'modules/utils/cli_utils.py'. We override at the class level to use .+
    """
    from mcp.server.fastmcp.resources.templates import ResourceTemplate

    def _matches_with_slashes(self, uri: str):
        pattern = _re.sub(r'\{([^}]+)\}', r'(?P<\1>.+)', self.uri_template)
        match = _re.match(f"^{pattern}$", uri)
        if match:
            return match.groupdict()
        return None

    ResourceTemplate.matches = _matches_with_slashes

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".pyc", ".pyo", ".class", ".jar",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".sqlite", ".db", ".pb",
})

_IGNORE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build",
    ".eggs", "*.egg-info",
})

_MAX_FILE_SIZE = 1_048_576  # 1 MB


def _is_safe_repo_path(path: Path) -> bool:
    """Ensure path is within workspace and not a symlink escape."""
    resolved = path.resolve()
    try:
        resolved.relative_to(_WORKSPACE_ROOT)
        return True
    except ValueError:
        return False


def _should_skip_dir(name: str) -> bool:
    """Check if a directory name should be excluded from listings."""
    return name in _IGNORE_DIRS or name.endswith(".egg-info")


@mcp.resource(
    "repo://tree/{path}",
    name="repo_tree",
    title="Repository directory listing",
    description="List files and subdirectories at a path in the repository. "
                "Use empty path or '.' for the root.",
    mime_type="application/json",
)
def repo_tree(path: str = ".") -> str:
    target = (_WORKSPACE_ROOT / path).resolve()
    if not _is_safe_repo_path(target):
        return json.dumps({"error": "Path is outside the repository"})
    if not target.is_dir():
        return json.dumps({"error": f"Not a directory: {path}"})

    entries = []
    try:
        for item in sorted(target.iterdir()):
            if item.name.startswith(".") and item.is_dir():
                if item.name in (".github", ".claude"):
                    pass  # include these
                else:
                    continue
            if item.is_dir() and _should_skip_dir(item.name):
                continue

            rel = item.relative_to(_WORKSPACE_ROOT)
            if item.is_dir():
                entries.append({"name": item.name, "type": "directory", "path": str(rel)})
            else:
                size = item.stat().st_size
                entries.append({
                    "name": item.name,
                    "type": "file",
                    "path": str(rel),
                    "size": size,
                })
    except PermissionError:
        return json.dumps({"error": f"Permission denied: {path}"})

    return json.dumps({
        "directory": str(target.relative_to(_WORKSPACE_ROOT)) if target != _WORKSPACE_ROOT else ".",
        "entries": entries,
        "count": len(entries),
    }, indent=2)


@mcp.resource(
    "repo://file/{path}",
    name="repo_file",
    title="Repository file content",
    description="Read the contents of a file in the repository. "
                "Returns text content for text files, metadata for binary files.",
    mime_type="text/plain",
)
def repo_file(path: str) -> str:
    target = (_WORKSPACE_ROOT / path).resolve()
    if not _is_safe_repo_path(target):
        return json.dumps({"error": "Path is outside the repository"})
    if not target.is_file():
        return json.dumps({"error": f"Not a file: {path}"})

    if target.stat().st_size > _MAX_FILE_SIZE:
        return json.dumps({
            "error": f"File too large ({target.stat().st_size:,} bytes, limit {_MAX_FILE_SIZE:,})",
            "path": path,
        })

    suffix = target.suffix.lower()
    if suffix in _BINARY_EXTENSIONS:
        return json.dumps({
            "type": "binary",
            "path": path,
            "size": target.stat().st_size,
            "extension": suffix,
            "note": "Binary file — content not returned. Use the path with gemini_prompt @ref if needed.",
        })

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return target.read_text(encoding="latin-1")
        except Exception:
            return json.dumps({"error": f"Could not decode file: {path}", "type": "binary"})


@mcp.resource(
    "repo://search/{pattern}",
    name="repo_search",
    title="Repository file search",
    description="Search for files by glob pattern (e.g. '**/*.py', 'modules/**/*.py'). "
                "Returns matching file paths relative to the repo root.",
    mime_type="application/json",
)
def repo_search(pattern: str) -> str:
    if not pattern or not pattern.strip():
        return json.dumps({"error": "Pattern cannot be empty"})

    matches = []
    limit = 500
    for match in _WORKSPACE_ROOT.glob(pattern):
        if not match.is_file():
            continue
        if not _is_safe_repo_path(match):
            continue
        parts = match.relative_to(_WORKSPACE_ROOT).parts
        if any(_should_skip_dir(p) for p in parts[:-1]):
            continue
        matches.append({
            "path": str(match.relative_to(_WORKSPACE_ROOT)),
            "size": match.stat().st_size,
        })
        if len(matches) >= limit:
            break

    return json.dumps({
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": len(matches) >= limit,
    }, indent=2)


@mcp.resource(
    "repo://grep/{query}",
    name="repo_grep",
    title="Repository content search",
    description="Search file contents for a text pattern (case-insensitive substring match). "
                "Returns matching lines with file paths and line numbers.",
    mime_type="application/json",
)
def repo_grep(query: str) -> str:
    if not query or not query.strip():
        return json.dumps({"error": "Query cannot be empty"})

    limit = 100
    results = _grep_with_git(query, limit) or _grep_fallback(query, limit)

    return json.dumps({
        "query": query,
        "results": results,
        "count": len(results),
        "truncated": len(results) >= limit,
    }, indent=2)


def _grep_with_git(query: str, limit: int) -> list[dict] | None:
    """Fast path: use git grep if inside a git repo."""
    try:
        result = _subprocess.run(
            ["git", "grep", "-inI", "--line-number", f"--max-count={limit}", "--", query],
            capture_output=True, text=True, timeout=30,
            cwd=str(_WORKSPACE_ROOT),
        )
        if result.returncode > 1:
            return None
    except (OSError, _subprocess.TimeoutExpired):
        return None

    results = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) >= 3:
            file_path, lineno_str, content = parts[0], parts[1], parts[2]
            try:
                results.append({
                    "file": file_path,
                    "line": int(lineno_str),
                    "content": content.rstrip()[:200],
                })
            except ValueError:
                continue
        if len(results) >= limit:
            break
    return results


def _grep_fallback(query: str, limit: int) -> list[dict]:
    """Fallback: Python-based search when git grep is unavailable."""
    results = []
    query_lower = query.lower()

    for root, dirs, files in os.walk(_WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d) and not d.startswith(".")]
        root_path = Path(root)

        for fname in files:
            fpath = root_path / fname
            suffix = fpath.suffix.lower()
            if suffix in _BINARY_EXTENSIONS:
                continue
            try:
                if fpath.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if query_lower in line.lower():
                            results.append({
                                "file": str(fpath.relative_to(_WORKSPACE_ROOT)),
                                "line": lineno,
                                "content": line.rstrip()[:200],
                            })
                            if len(results) >= limit:
                                return results
            except (PermissionError, OSError):
                continue

    return results


# ============================================================================
# Server Entry Point
# ============================================================================

def main():
    """Run the MCP server."""
    logger.info("Starting Antigravity CLI MCP Server")
    logger.info(f"Log level: {log_level}")

    if validate_cli_setup():
        logger.info("Antigravity CLI setup validated successfully")
    else:
        logger.warning("Antigravity CLI not found - some features may not work")

    _patch_template_matching()
    mcp.run()


if __name__ == "__main__":
    main()
