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
    CLIExecutionError,
    CLITimeoutError,
    CLIRateLimitError,
)
from modules.config.cli_config import (
    GEMINI_COLLABORATION_LIMIT,
)

logger = logging.getLogger(__name__)

# Default model selections by mode (agy-only; no OpenRouter models)
DEFAULT_MODELS = {
    "sequential": "gemini-2.5-flash",
    "debate": "gemini-2.5-flash",
    "validation": "gemini-2.5-flash",
}


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
        models: Comma-separated model list (ignored; agy manages models internally)
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

    # Get default models for mode if not specified
    if not models:
        models = DEFAULT_MODELS.get(collaboration_mode, DEFAULT_MODELS["sequential"])

    model_list = [m.strip() for m in models.split(",")]

    # Route to appropriate handler
    if collaboration_mode == "sequential":
        return await _execute_sequential(
            content=content,
            models=model_list,
            context=context,
            pipeline_stages=pipeline_stages,
            handoff_criteria=handoff_criteria,
            quality_gates=quality_gates,
            focus=focus
        )
    elif collaboration_mode == "debate":
        return await _execute_debate(
            content=content,
            models=model_list,
            context=context,
            rounds=rounds,
            debate_style=debate_style,
            convergence_criteria=convergence_criteria,
            focus=focus
        )
    elif collaboration_mode == "validation":
        return await _execute_validation(
            content=content,
            models=model_list,
            context=context,
            validation_criteria=validation_criteria,
            confidence_threshold=confidence_threshold,
            consensus_method=consensus_method,
            conflict_resolution=conflict_resolution,
            focus=focus
        )
    else:
        return json.dumps({
            "status": "error",
            "error": f"Unknown collaboration mode: {collaboration_mode}",
            "valid_modes": ["sequential", "debate", "validation"]
        })


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

        # Execute with appropriate model (ignored by agy, kept for logging)
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
        "model_ignored": True,
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
        "model_ignored": True,
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
        "model_ignored": True,
    }, indent=2)


async def _execute_model(model: str, prompt: str) -> dict:
    """Execute a prompt with the specified model via agy."""
    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(prompt=cleaned_prompt, files=files)

    try:
        result = await execute_cli_with_retry(args)
        return {
            "status": "success",
            "content": result.get("stdout", ""),
            "source": "antigravity_cli",
            "model_ignored": True,
        }
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

        result = await _execute_model("gemini-2.5-flash", prompt)
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

        result = await _execute_model("gemini-2.5-flash", prompt)
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

        result = await _execute_model("gemini-2.5-flash", prompt)
        return result.get("content", result.get("stdout", "Consensus reached"))
    except Exception as e:
        logger.error(f"Error building consensus: {e}")
        return "Validation completed"
