"""
AI collaboration system with advanced workflow modes.

This module implements the gemini_ai_collaboration tool with support for
sequential, debate, and validation collaboration modes.
"""
import json
import logging
from typing import Optional

from modules.utils.cli_utils import (
    execute_cli_with_retry,
    extract_file_refs,
    _build_cli_args,
    drop_unavailable_default,
    validate_model,
    add_model_metadata,
    CLIExecutionError,
    CLITimeoutError,
    CLIRateLimitError,
)
from modules.config.cli_config import (
    GEMINI_COLLABORATION_LIMIT,
    get_task_timeout,
)

logger = logging.getLogger(__name__)

# Stable model slugs (agy >= 1.1.5). The short names these replaced
# ("flash", "pro", "claude") were dropped in agy 1.1.4 and now hard-fail, which
# meant every default collaboration run errored out. The same happened again
# when agy 1.5.3 dropped gemini-3.1-pro-high; its High-tier role is now taken by
# gemini-3.8-flash-high, and a default that disappears from `agy models` is now
# dropped at run time with a warning (see execute_collaboration).
DEFAULT_MODELS = {
    "sequential": "gemini-3.8-flash-medium,gemini-3.8-flash-high",
    "debate": "gemini-3.8-flash-high,gemini-3.8-flash-medium,claude-sonnet-4-6",
    "validation": "gemini-3.8-flash-high,gemini-3.8-flash-medium",
}

# Model used for the cheap aggregation steps (pipeline summary, debate
# synthesis, consensus building) rather than the per-stage reasoning.
SYNTHESIS_MODEL = "gemini-3.8-flash-medium"


async def execute_collaboration(
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
    Execute multi-platform AI collaboration.

    Args:
        collaboration_mode: Mode (sequential, debate, validation)
        content: Content to analyze
        models: Comma-separated model list of slugs or display names
                (e.g., "gemini-3.8-flash-high,claude-sonnet-4-6" for debate)
        context: Additional context
        Other mode-specific parameters

    Returns:
        JSON string with collaboration results
    """
    if len(content) > GEMINI_COLLABORATION_LIMIT:
        return json.dumps({
            "status": "error",
            "error": f"Content exceeds limit of {GEMINI_COLLABORATION_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        })

    # Checked before any model resolution, which may run `agy models`.
    if collaboration_mode not in DEFAULT_MODELS:
        return json.dumps({
            "status": "error",
            "error": f"Unknown collaboration mode: {collaboration_mode}",
            "valid_modes": list(DEFAULT_MODELS),
        })

    # Get default models for mode if not specified. Only shipped defaults are
    # checked against the live roster; a list the caller passed must fail
    # loudly on an unknown model rather than quietly run a different one.
    # SYNTHESIS_MODEL is always a shipped default, so it is checked either way
    # (the synthesis steps resolve it again through _synthesis_model()).
    default_notes: list[dict] = []

    def _note(meta: dict) -> None:
        # The same model can be both a stage default and the synthesis model.
        if meta and meta not in default_notes:
            default_notes.append(meta)

    if models:
        model_list = [m.strip() for m in models.split(",")]
    else:
        model_list = []
        for default in DEFAULT_MODELS[collaboration_mode].split(","):
            # A dropped default becomes "": _execute_model then lets agy pick.
            resolved, note = await drop_unavailable_default(default.strip())
            model_list.append(resolved or "")
            _note(note)
    _note((await drop_unavailable_default(SYNTHESIS_MODEL))[1])

    if collaboration_mode == "sequential":
        output = await _execute_sequential(
            content=content,
            models=model_list,
            context=context,
            pipeline_stages=pipeline_stages,
            handoff_criteria=handoff_criteria,
            quality_gates=quality_gates,
            focus=focus
        )
    elif collaboration_mode == "debate":
        output = await _execute_debate(
            content=content,
            models=model_list,
            context=context,
            rounds=rounds,
            debate_style=debate_style,
            convergence_criteria=convergence_criteria,
            focus=focus
        )
    else:  # "validation" (the mode was validated above)
        output = await _execute_validation(
            content=content,
            models=model_list,
            context=context,
            validation_criteria=validation_criteria,
            confidence_threshold=confidence_threshold,
            consensus_method=consensus_method,
            conflict_resolution=conflict_resolution,
            focus=focus
        )

    if not default_notes:
        return output
    merged = json.loads(output)
    for note in default_notes:
        add_model_metadata(merged, note)
    return json.dumps(merged, indent=2)


async def _execute_sequential(
    content: str,
    models: list[str],
    context: Optional[str],
    pipeline_stages: Optional[str],
    handoff_criteria: str,
    quality_gates: str,
    focus: Optional[str]
) -> str:
    """Execute sequential pipeline collaboration."""
    # Parse stages
    if pipeline_stages:
        stages = [s.strip() for s in pipeline_stages.split(",")]
    else:
        # Auto-generate stages based on model count
        default_stages = ["analysis", "security_review", "optimization", "final_validation"]
        stages = default_stages[:len(models)]

    results = []
    previous_output = None

    for i, (model, stage) in enumerate(zip(models, stages)):
        try:
            from prompts.sequential_template import get_sequential_stage_prompt
            prompt = get_sequential_stage_prompt(
                content=content,
                stage_name=stage,
                stage_number=i + 1,
                total_stages=len(stages),
                previous_output=previous_output,
                focus=focus
            )
        except ImportError:
            prompt = f"""Stage {i + 1}/{len(stages)}: {stage}

Previous output: {previous_output or 'None'}

Content:
{content}

Perform {stage} analysis."""

        # Execute with the model assigned to this pipeline stage
        result = await _execute_model(model, prompt)

        results.append({
            "stage": stage,
            "stage_number": i + 1,
            "model": model,
            "result": result
        })

        # Use output for next stage
        previous_output = result.get("content", result.get("stdout", ""))

    # Generate summary
    summary = await _generate_pipeline_summary(content, results, stages)

    return json.dumps({
        "status": "success",
        "collaboration_mode": "sequential",
        "stages_completed": len(results),
        "results": results,
        "summary": summary,
    }, indent=2)


async def _execute_debate(
    content: str,
    models: list[str],
    context: Optional[str],
    rounds: int,
    debate_style: str,
    convergence_criteria: str,
    focus: Optional[str]
) -> str:
    """Execute debate collaboration."""
    all_arguments = []

    for round_num in range(1, rounds + 1):
        round_results = []
        previous_args = "\n\n".join(
            f"[{a['model']}]: {a['argument']}"
            for a in all_arguments
        ) if all_arguments else None

        for model in models:
            try:
                from prompts.debate_template import get_debate_prompt
                prompt = get_debate_prompt(
                    content=content,
                    round_number=round_num,
                    total_rounds=rounds,
                    debate_style=debate_style,
                    previous_arguments=previous_args,
                    focus=focus
                )
            except ImportError:
                prompt = f"""Debate round {round_num}/{rounds} ({debate_style} style)

Topic: {content}

Previous arguments: {previous_args or 'None'}

Provide your perspective."""

            result = await _execute_model(model, prompt)

            argument = result.get("content", result.get("stdout", ""))
            round_results.append({
                "model": model,
                "round": round_num,
                "argument": argument
            })
            all_arguments.append({
                "model": model,
                "round": round_num,
                "argument": argument
            })

    # Generate synthesis
    synthesis = await _generate_debate_synthesis(
        content, all_arguments, debate_style, rounds
    )

    return json.dumps({
        "status": "success",
        "collaboration_mode": "debate",
        "debate_style": debate_style,
        "rounds_completed": rounds,
        "arguments": all_arguments,
        "synthesis": synthesis,
    }, indent=2)


async def _execute_validation(
    content: str,
    models: list[str],
    context: Optional[str],
    validation_criteria: Optional[str],
    confidence_threshold: float,
    consensus_method: str,
    conflict_resolution: str,
    focus: Optional[str]
) -> str:
    """Execute validation collaboration."""
    # Auto-generate criteria if not provided
    if not validation_criteria:
        validation_criteria = "code_quality,performance,security,maintainability"

    validations = []

    for model in models:
        try:
            from prompts.validation_template import get_validation_prompt
            prompt = get_validation_prompt(
                content=content,
                validation_criteria=validation_criteria,
                confidence_threshold=confidence_threshold,
                context=context
            )
        except ImportError:
            prompt = f"""Validate the following against criteria: {validation_criteria}

Confidence threshold: {confidence_threshold}

Content:
{content}

Provide validation results."""

        result = await _execute_model(model, prompt)

        validations.append({
            "model": model,
            "validation": result.get("content", result.get("stdout", ""))
        })

    # Build consensus
    consensus = await _build_consensus(
        content, validations, validation_criteria, consensus_method, conflict_resolution
    )

    return json.dumps({
        "status": "success",
        "collaboration_mode": "validation",
        "validation_criteria": validation_criteria,
        "confidence_threshold": confidence_threshold,
        "validations": validations,
        "consensus": consensus,
    }, indent=2)


async def _synthesis_model() -> Optional[str]:
    """
    SYNTHESIS_MODEL, or None (agy's default) if agy no longer offers it.

    The warning is dropped here because execute_collaboration already added the
    same check's warning to the response (the roster is cached, so both calls
    see the same list).
    """
    model, _ = await drop_unavailable_default(SYNTHESIS_MODEL)
    return model


async def _execute_model(model: Optional[str], prompt: str) -> dict:
    """Execute a prompt with the specified model via agy."""
    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files, model=model or None)

    try:
        result = await execute_cli_with_retry(args, timeout=get_task_timeout("ai_collaboration"))
        if result.get("status") == "error":
            return {
                "status": "error",
                "error": result.get("stdout", "") or result.get("stderr", ""),
                "source": "antigravity_cli",
            }
        return add_model_metadata(
            {
                "status": "success",
                "content": result.get("stdout", ""),
                "source": "antigravity_cli",
                "model": model,
            },
            await validate_model(model or None),
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def _generate_pipeline_summary(
    original_content: str,
    results: list[dict],
    stages: list[str]
) -> str:
    """Generate a summary of pipeline results."""
    try:
        from prompts.sequential_template import get_pipeline_summary_prompt
        all_outputs = "\n\n---\n\n".join(
            f"Stage {r['stage']}:\n{r['result'].get('content', r['result'].get('stdout', ''))}"
            for r in results
        )
        prompt = get_pipeline_summary_prompt(original_content, all_outputs, stages)

        result = await _execute_model(await _synthesis_model(), prompt)
        return result.get("content", result.get("stdout", "Pipeline complete"))
    except Exception as e:
        logger.error(f"Error generating pipeline summary: {e}")
        return "Pipeline completed successfully"


async def _generate_debate_synthesis(
    topic: str,
    all_arguments: list[dict],
    debate_style: str,
    total_rounds: int
) -> str:
    """Generate synthesis of debate arguments."""
    try:
        from prompts.debate_template import get_debate_synthesis_prompt
        args_text = "\n\n".join(
            f"[Round {a['round']} - {a['model']}]:\n{a['argument']}"
            for a in all_arguments
        )
        prompt = get_debate_synthesis_prompt(topic, args_text, debate_style, total_rounds)

        result = await _execute_model(await _synthesis_model(), prompt)
        return result.get("content", result.get("stdout", "Debate concluded"))
    except Exception as e:
        logger.error(f"Error generating debate synthesis: {e}")
        return "Debate completed"


async def _build_consensus(
    content: str,
    validations: list[dict],
    validation_criteria: str,
    consensus_method: str,
    conflict_resolution: str
) -> str:
    """Build consensus from validation results."""
    try:
        from prompts.validation_template import get_consensus_prompt
        all_validations = "\n\n---\n\n".join(
            f"[{v['model']}]:\n{v['validation']}"
            for v in validations
        )
        prompt = get_consensus_prompt(
            content, all_validations, validation_criteria, consensus_method, conflict_resolution
        )

        result = await _execute_model(await _synthesis_model(), prompt)
        return result.get("content", result.get("stdout", "Consensus reached"))
    except Exception as e:
        logger.error(f"Error building consensus: {e}")
        return "Validation completed"
