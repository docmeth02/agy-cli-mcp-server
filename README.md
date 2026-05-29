# Antigravity CLI MCP Server

A Model Context Protocol (MCP) server that bridges Google's **Antigravity CLI** (`agy`) with MCP-compatible clients like Claude Code and Claude Desktop. It provides 24 specialized tools for AI-assisted workflows.

> Forked from [centminmod/gemini-cli-mcp-server](https://github.com/centminmod/gemini-cli-mcp-server). Refactored from the deprecated Google Gemini CLI to use **Antigravity CLI** (`agy`). Tool names retain the `gemini_` prefix for backward compatibility.

## 🚀 Key Features

- **24 Specialized MCP Tools** - Complete toolset for AI-assisted workflows across 5 tool categories
- **Antigravity CLI Integration** - Direct bridge to Google's `agy` CLI with native conversation support
- **Enterprise Architecture** - Refactored modular design with specialized modules
- **Conversation History** - Stateful multi-turn conversations via agy native `.pb` files
- **Dynamic Token Limits** - Tool-specific limits from 100K-800K characters
- **Multi-AI Workflows** - Purpose-built tools for plan evaluation, code review, and collaboration
- **@filename Support** - Direct file reading with intelligent expansion for 23 tools
- **Enterprise Security** - Multi-layer defense with real-time protection
- **Production Ready** - Async architecture with retry logic and comprehensive error handling
- **High Concurrency** - Async architecture supporting 1,000-10,000+ concurrent requests

## 📋 Table of Contents

- [Architecture Overview](#%EF%B8%8F-architecture-overview)
- [Tool Suite](#%EF%B8%8F-tool-suite)
  - [Core Gemini Tools](#core-gemini-tools-6)
  - [System Tools](#system-tools-3)
  - [Analysis Tools](#analysis-tools-5)
  - [Conversation Tools](#conversation-tools-5)
  - [Specialized Code Review Tools](#specialized-code-review-tools-3)
  - [Content Comparison Tools](#content-comparison-tools-1)
  - [AI Collaboration Tools](#ai-collaboration-tools-1)
- [Installation](#-installation)
- [MCP Client Configuration](#%EF%B8%8F-mcp-client-configuration)
- [Usage Examples](#-usage-examples)
- [Advanced Features](#-advanced-features)
- [Configuration](#%EF%B8%8F-configuration)
- [Performance](#-performance)
- [Testing](#-testing)
- [Troubleshooting](#-troubleshooting)

## 🏗️ Architecture Overview

The Gemini CLI MCP Server features a modular, enterprise-grade architecture designed for reliability, performance, and maintainability.

```text
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Claude Code   │ ← → │   MCP Protocol   │ ← → │  Antigravity    │
│   MCP Client    │    │   (JSON-RPC 2.0) │    │   CLI (agy)     │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         ↑                       ↑                       ↑
    ┌─────────┐            ┌─────────────┐         ┌─────────────┐
    │ 24 MCP  │            │ FastMCP     │         │ Google      │
    │ Tools   │            │ Server      │         │ Gemini AI   │
    └─────────┘            └─────────────┘         └─────────────┘
```

### Core Components

**Core Server Layer (6 modules):**
- **`mcp_server.py`** - Streamlined main coordinator with tool registration pattern
- **`modules/core/mcp_core_tools.py`** - Pure MCP tool implementations for core agy tools
- **`modules/core/mcp_collaboration_engine.py`** - AI collaboration system with advanced workflow modes
- **`modules/core/mcp_service_implementations.py`** - System and service tools coordination layer
- **`modules/core/mcp_code_review_tools.py`** - Specialized code review and analysis tools
- **`modules/core/mcp_content_comparison_tools.py`** - Multi-source content comparison capabilities

**Configuration & Infrastructure (3 modules):**
- **`modules/config/cli_config.py`** - Main configuration interface with env-var parsing
- **`modules/utils/cli_utils.py`** - Core utilities: subprocess execution, `@filename` expansion, TTL caching
- **`modules/services/conversation_manager.py`** - Agy-native conversation management with JSON metadata sidecar

**Template System (15 modules):**
- **`prompts/`** - Template module with TTL caching and integrity verification
- Core templates: template_loader.py, base_template.py, eval_template.py, review_template.py, verify_template.py, summarize_template.py
- Collaboration templates: debate_template.py, sequential_template.py, validation_template.py
- Code review templates: code_review_template.py, extract_structured_template.py, git_diff_review_template.py
- Content analysis templates: content_comparison_template.py
- Plus interface and supporting files

**Security Framework (5 modules):**
- **`security/`** - Enterprise security framework
- Includes: credential_sanitizer.py, pattern_detector.py, security_monitor.py, security_validator.py, jsonrpc_validator.py

**Key Architectural Decisions:**

- **FastMCP Framework**: Official MCP Python SDK with JSON-RPC 2.0 compliance
- **Direct Subprocess Execution**: Avoids shell injection vulnerabilities by using `subprocess` directly (not shell)
- **@filename Server-Side Expansion**: `extract_file_refs()` parses prompts for `@path` tokens, expands globs, and converts them to `--add-dir` flags for agy
- **Agy-Native Conversations**: Conversation state managed by agy's native `.pb` protobuf files; metadata tracked in JSON sidecar
- **Structured Error Classification**: Error detection scans stdout for `^Error:`, `^CLI error:`, `^Warning: conversation "..." not found` patterns (agy always exits 0)
- **Multi-Tier TTL Caching**: Different cache durations optimized for each use case
- **Full Async/Await**: High-concurrency architecture supporting 1,000-10,000+ requests
- **Exponential Backoff Retry**: Intelligent retry logic with jitter for transient errors
- **Input Validation**: Multi-layer validation with length limits and sanitization
- **Information Disclosure Prevention**: Sanitized client responses with detailed server logging

## 🛠️ Tool Suite

The server provides 24 specialized MCP tools organized into five categories:

### Core Gemini Tools (6)

#### `gemini_cli`
Execute any Antigravity CLI command directly with comprehensive error handling.
```python
gemini_cli(command="--print 'Hello world'")
gemini_cli(command="--print 'Explain AI'")
```

#### `gemini_help`
Get cached agy help information (30-minute TTL).
```python
gemini_help()
```

#### `gemini_version`
Get cached agy version information (30-minute TTL).
```python
gemini_version()
```

#### `gemini_prompt`
Send prompts with structured parameters and validation (100,000 char limit).
```python
gemini_prompt(
    prompt="Explain quantum computing",
    sandbox=False,
    debug=False
)
```

#### `gemini_models`
List all available Gemini AI models.
```python
gemini_models()
```

#### `gemini_metrics`
Get comprehensive server performance metrics and statistics.
```python
gemini_metrics()
```

### System Tools (3)

#### `gemini_sandbox`
Execute prompts in sandbox mode for code execution (200,000 char limit).
```python
gemini_sandbox(
    prompt="Write and run a Python script to analyze data"
)
```

#### `gemini_cache_stats`
Get cache statistics for all cache backends.
```python
gemini_cache_stats()
```

#### `gemini_rate_limiting_stats`
Get comprehensive rate limiting and quota statistics.
```python
gemini_rate_limiting_stats()
```

### Analysis Tools (5)

#### `gemini_summarize`
Summarize content with focus-specific analysis (400,000 char limit).
```python
gemini_summarize(
    content="Your code or text content here",
    focus="architecture and design patterns"
)
```

#### `gemini_summarize_files`
File-based summarization optimized for @filename syntax (800,000 char limit).

**Key Advantages over `gemini_summarize`**:
- **2x Higher Limit**: 800K vs 400K characters for large codebases
- **@filename Optimized**: Purpose-built for direct file reading
- **Token Efficiency**: 50-70% improvement with lightweight prompts
- **Enterprise Scale**: Handles massive multi-directory projects

```python
gemini_summarize_files(
    files="@src/ @docs/ @tests/",  # @filename syntax
    focus="complete system analysis"  # optional
)
```

#### `gemini_eval_plan`
Evaluate Claude Code implementation plans (500,000 char limit).
```python
gemini_eval_plan(
    plan="Implementation plan from Claude Code",
    context="Node.js REST API with MongoDB",
    requirements="Must support 10,000 concurrent users"
)
```

#### `gemini_review_code`
Review specific code suggestions with detailed analysis (300,000 char limit).
```python
gemini_review_code(
    code="Code snippet or @filename to review",
    purpose="Security review of authentication",
    context="Express.js REST API",
    language="javascript"
)
```

#### `gemini_verify_solution`
Comprehensive verification of complete solutions (800,000 char limit).
```python
gemini_verify_solution(
    solution="Complete implementation including code, tests, docs",
    requirements="Original requirements specification",
    test_criteria="Performance and security criteria",
    context="Production deployment environment"
)
```

### Conversation Tools (5)

#### `gemini_start_conversation`
Start a new conversation with ID for stateful interactions.
```python
gemini_start_conversation(
    title="Python Development Help",
    description="Ongoing assistance with Python project",
    tags="python,development",
    expiration_hours=24
)
```

#### `gemini_continue_conversation`
Continue an existing conversation with context history.
```python
gemini_continue_conversation(
    conversation_id="conv_12345",
    prompt="How do I optimize this function?"
)
```

#### `gemini_list_conversations`
List active conversations with metadata.
```python
gemini_list_conversations(
    limit=20,
    status_filter="active"
)
```

#### `gemini_clear_conversation`
Clear/delete a specific conversation.
```python
gemini_clear_conversation(conversation_id="conv_12345")
```

#### `gemini_conversation_stats`
Get conversation system statistics and health.
```python
gemini_conversation_stats()
```

### Specialized Code Review Tools (3)

#### `gemini_code_review`
Comprehensive code analysis with structured output.
```python
gemini_code_review(
    code="Your code to review",
    language="python",  # optional, auto-detected
    focus_areas="security,performance,quality,best_practices",  # optional
    severity_threshold="info",  # optional: info, warning, error, critical
    output_format="structured"  # optional: structured, markdown, json
)
```

#### `gemini_extract_structured`
Extract structured data using JSON schemas.
```python
# Define a schema for code analysis
schema = {
    "type": "object",
    "properties": {
        "functions": {"type": "array"},
        "classes": {"type": "array"},
        "issues": {"type": "array"}
    }
}

gemini_extract_structured(
    content="Code or text to analyze",
    schema=json.dumps(schema),
    examples="Optional examples of expected output",  # optional
    strict_mode=True  # optional
)
```

#### `gemini_git_diff_review`
Analyze git diffs with contextual feedback.
```python
gemini_git_diff_review(
    diff="Git diff content or patch",
    context_lines=3,  # optional
    review_type="comprehensive",  # optional: comprehensive, security_only, performance_only, quick
    base_branch="main",  # optional
    commit_message="Fix authentication feature"  # optional
)
```

### Content Comparison Tools (1)

#### `gemini_content_comparison`
Advanced multi-source content comparison and analysis.
```python
# Compare documentation versions
gemini_content_comparison(
    sources='["@README.md", "@docs/README.md", "https://github.com/user/repo/README.md"]',
    comparison_type="semantic",  # semantic, textual, structural, factual, code
    output_format="structured",  # structured, matrix, summary, detailed, json
    include_metrics=True,        # optional, include similarity scores
    focus_areas="completeness,accuracy,structure"  # optional, what to focus on
)

# Code version analysis
gemini_content_comparison(
    sources='["@src/auth_v1.py", "@src/auth_v2.py"]',
    comparison_type="code", 
    output_format="detailed",
    focus_areas="differences,security,performance"
)
```

### AI Collaboration Tools (1)

#### `gemini_ai_collaboration`
Enhanced multi-platform AI collaboration.
```python
# Sequential analysis pipeline
gemini_ai_collaboration(
    collaboration_mode="sequential",
    content="Your task or code to analyze",
    pipeline_stages="analysis,security_review,optimization,final_validation"
)

# Multi-round AI debate
gemini_ai_collaboration(
    collaboration_mode="debate", 
    content="Should we use microservices or monolith?",
    rounds=4,
    debate_style="constructive"
)
```

#### Complete Parameter Reference for `gemini_ai_collaboration`

**Available Collaboration Modes:**
- **`sequential`** - Progressive refinement through ordered analysis pipeline
- **`debate`** - Multi-round discussions with consensus building  
- **`validation`** - Cross-platform validation with conflict resolution

**Available Debate Styles (for debate mode):**
- **`constructive`** (default) - Focus on building understanding rather than winning arguments
- **`adversarial`** - Challenge assumptions and arguments rigorously  
- **`collaborative`** - Work together to explore topics comprehensively
- **`socratic`** - Use questioning to explore underlying assumptions
- **`devil_advocate`** - Deliberately argue for challenging positions

**Universal Parameters:**
- **`collaboration_mode`** (required): `sequential` | `debate` | `validation`
- **`content`** (required): Content to be analyzed/processed
- **`models`** (optional): Comma-separated list of AI models (auto-selected if not provided; note: `model` parameter is accepted but ignored by agy)
- **`context`** (optional): Additional context for collaboration
- **`conversation_id`** (optional): For stateful conversation history

**Sequential Mode Parameters:**
- **`pipeline_stages`** (optional): Comma-separated stages (auto-generated if not provided)
- **`handoff_criteria`** (optional): `completion_of_stage` | `quality_threshold` | `consensus_reached` | `time_based`
- **`quality_gates`** (optional): `none` | `basic` | `standard` | `strict` | `comprehensive`
- **`focus`** (optional): Focus area (default: "progressive refinement")

**Debate Mode Parameters:**  
- **`rounds`** (optional): Number of debate rounds (1-10, default: 3)
- **`debate_style`** (optional): See debate styles above (default: "constructive")
- **`convergence_criteria`** (optional): `substantial_agreement` | `consensus` | `majority_view` | `all_viewpoints`
- **`focus`** (optional): Focus area (default: "comprehensive analysis")

**Validation Mode Parameters:**
- **`validation_criteria`** (optional): Comma-separated criteria (auto-generated if not provided)
- **`confidence_threshold`** (optional): 0.0-1.0 (default: 0.7)
- **`consensus_method`** (optional): `simple_majority` | `weighted_majority` | `unanimous` | `supermajority` | `expert_panel`
- **`conflict_resolution`** (optional): `ignore` | `flag_only` | `detailed_analysis` | `additional_validation` | `expert_arbitration`

#### Advanced Usage Examples

**Different Debate Styles:**
```python
# Adversarial debate for critical analysis
gemini_ai_collaboration(
    collaboration_mode="debate",
    content="Should our startup use microservices architecture?",
    rounds=3,
    debate_style="adversarial",
    convergence_criteria="majority_view"
)

# Socratic questioning for deep exploration
gemini_ai_collaboration(
    collaboration_mode="debate",
    content="What makes code maintainable?",
    rounds=4,
    debate_style="socratic",
    focus="fundamental principles"
)

# Devil's advocate for stress testing ideas
gemini_ai_collaboration(
    collaboration_mode="debate",
    content="Our new feature implementation plan",
    rounds=2,
    debate_style="devil_advocate",
    focus="identifying potential failures"
)
```

**Sequential Pipeline Examples:**
```python
# Quality-gated sequential analysis
gemini_ai_collaboration(
    collaboration_mode="sequential",
    content="@src/authentication.py",
    pipeline_stages="code_review,security_analysis,performance_optimization,documentation",
    quality_gates="strict",
    handoff_criteria="quality_threshold"
)

# Time-based handoffs for rapid iteration
gemini_ai_collaboration(
    collaboration_mode="sequential",
    content="Product requirements analysis",
    pipeline_stages="initial_analysis,stakeholder_review,final_recommendations",
    handoff_criteria="time_based",
    focus="user experience"
)
```

**Validation Examples:**
```python
# High-confidence consensus validation
gemini_ai_collaboration(
    collaboration_mode="validation",
    content="Critical system design decisions",
    validation_criteria="scalability,security,maintainability,cost_efficiency",
    confidence_threshold=0.9,
    consensus_method="unanimous",
    conflict_resolution="expert_arbitration"
)

# Supermajority validation with detailed conflict analysis
gemini_ai_collaboration(
    collaboration_mode="validation",
    content="API design specification",
    validation_criteria="usability,performance,consistency,documentation",
    consensus_method="supermajority",
    conflict_resolution="detailed_analysis"
)
```

## 📦 Installation

### Prerequisites

- **Python 3.10+** - Required for MCP SDK compatibility
- **Antigravity CLI** - Google's command-line tool for Gemini AI (`agy`)
- **uv** (recommended) or pip for package management

### Linux Setup

```bash
# Install uv (recommended package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone https://github.com/docmeth02/agy-cli-mcp-server.git
cd agy-cli-mcp-server

# Create and activate virtual environment
uv venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt

# Install and configure Antigravity CLI
npm install -g @google-ai/antigravity-cli
agy login

# Verify installation
agy --version
python mcp_server.py --help
```

### macOS Setup

```bash
# Install uv via Homebrew (or use curl installer above)
brew install uv

# Clone the repository
git clone https://github.com/docmeth02/agy-cli-mcp-server.git
cd agy-cli-mcp-server

# Create and activate virtual environment
uv venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt

# Install Antigravity CLI (if not already installed)
npm install -g @google-ai/antigravity-cli

# Authenticate
agy login

# Verify installation
agy --version
python mcp_server.py --help
```

### Alternative Installation (pip)

```bash
# Using standard Python tools
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Authenticate Antigravity CLI
agy login
```

## MCP Client Configuration

### Claude Code (recommended)

After cloning and installing dependencies (see Installation above), register the server:

```bash
# From the repo directory — registers globally for all projects
claude mcp add agy-cli "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py" -s user
```

Scope options (`-s`):
* `user` — available across all your projects (recommended)
* `local` — available only to you in the current project
* `project` — shared with everyone via `.mcp.json` (committed to repo)

Verify it works:
```bash
claude mcp list
```

### Claude Desktop

Add to your settings file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "agy-cli": {
      "command": "/absolute/path/to/agy-cli-mcp-server/.venv/bin/python",
      "args": ["/absolute/path/to/agy-cli-mcp-server/mcp_server.py"]
    }
  }
}
```

Use absolute paths for both the Python executable and `mcp_server.py`.

### Other MCP Clients

Use stdio transport with the venv Python executable and `mcp_server.py` as the argument.

## 🎯 Usage Examples

### Basic Operations

**Simple Q&A**:
```python
# Quick question
@gemini_prompt("What is machine learning?")

# Complex analysis
@gemini_prompt("Analyze the trade-offs between REST and GraphQL APIs")
```

**File Analysis**:
```python
# Review code file directly
@gemini_review_code(
    code="@src/auth.py",
    purpose="Security vulnerability assessment",
    language="python"
)

# Summarize multiple files (standard approach)
@gemini_summarize(
    content="@src/ @tests/ @docs/",
    focus="architecture and design patterns"
)

# Large-scale file analysis (optimized approach)
@gemini_summarize_files(
    files="@src/ @lib/ @components/ @tests/ @docs/",
    focus="complete system architecture and dependencies"
)
```

**Code Execution**:
```python
# Interactive development
@gemini_sandbox(
    prompt="Create a data visualization of sales trends"
)
```

### Dual-AI Workflow Examples

The dual-AI workflow enables powerful collaboration between Claude Code and Gemini AI:

#### 1. Plan Evaluation
```python
# Claude Code generates an implementation plan
plan = """
1. Create JWT authentication middleware
2. Implement rate limiting
3. Add input validation with Joi
4. Set up comprehensive error handling
5. Create user registration/login endpoints
"""

# Gemini AI evaluates the plan
@gemini_eval_plan(
    plan=plan,
    context="Express.js REST API for e-commerce platform",
    requirements="Must support 50,000 concurrent users, GDPR compliant"
)
```

#### 2. Code Review During Development
```python
# Claude Code suggests implementation
code = """
const jwt = require('jsonwebtoken');

const authMiddleware = (req, res, next) => {
    const token = req.header('Authorization')?.replace('Bearer ', '');
    
    if (!token) {
        return res.status(401).json({ error: 'Access denied' });
    }
    
    try {
        const decoded = jwt.verify(token, process.env.JWT_SECRET);
        req.user = decoded;
        next();
    } catch (error) {
        res.status(401).json({ error: 'Invalid token' });
    }
};
"""

# Gemini AI reviews the code
@gemini_review_code(
    code=code,
    purpose="JWT authentication middleware for Express.js",
    context="E-commerce API with high security requirements",
    language="javascript"
)
```

#### 3. Complete Solution Verification
```python
# Complete implementation ready for deployment
solution = """
[Complete implementation including:]
- Authentication system with JWT and refresh tokens
- Rate limiting middleware
- Input validation with comprehensive schemas
- Error handling with structured responses
- User management endpoints
- Security headers and CORS configuration
- Comprehensive test suite
- API documentation
- Deployment configuration
"""

# Final verification before deployment
@gemini_verify_solution(
    solution=solution,
    requirements="Secure authentication system with rate limiting",
    test_criteria="Handle 50k concurrent users, 99.9% uptime, sub-200ms response",
    context="Production deployment on AWS ECS"
)
```

### Advanced Usage Patterns

**Large Codebase Analysis**:
```python
# Enterprise-scale project analysis (recommended)
@gemini_summarize_files(
    files="@src/ @lib/ @components/ @utils/ @tests/ @docs/",
    focus="architectural patterns and dependencies"
)

# Alternative for smaller projects
@gemini_summarize(
    content="@src/ @lib/ @components/ @utils/ @tests/",
    focus="architectural patterns and dependencies"
)
```

**Performance Analysis**:
```python
# Review code for performance issues
@gemini_review_code(
    code="@src/api/handlers/ @src/database/",
    purpose="Performance optimization and bottleneck identification",
    context="High-traffic API serving 1M requests/day"
)
```

**Security Assessment**:
```python
# Comprehensive security review
@gemini_review_code(
    code="@auth/ @middleware/ @validators/",
    purpose="Security vulnerability assessment",
    context="Financial services application with PCI compliance requirements"
)
```

### Specialized Code Review Examples

**Structured Code Analysis**:
```python
# Comprehensive code review with structured output
@gemini_code_review(
    code="@src/api/handlers/",
    language="python",
    focus_areas="security,performance,maintainability",
    severity_threshold="warning",
    output_format="structured"
)
```

**Schema-Based Data Extraction**:
```python
# Extract API endpoints from codebase
schema = {
    "type": "object",
    "properties": {
        "endpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "method": {"type": "string"},
                    "authentication": {"type": "boolean"}
                }
            }
        }
    }
}

@gemini_extract_structured(
    content="@src/routes/",
    schema=json.dumps(schema),
    strict_mode=True
)
```

**Git Diff Analysis**:
```python
# Review pull request changes
@gemini_git_diff_review(
    diff="@pull_request.diff",
    review_type="comprehensive",
    base_branch="main",
    commit_message="Add user authentication feature"
)
```

**Multi-Source Content Comparison**:
```python
# Compare API documentation versions
@gemini_content_comparison(
    sources='["@docs/api_v1.md", "@docs/api_v2.md", "https://api.example.com/docs"]',
    comparison_type="semantic",
    output_format="matrix",
    focus_areas="breaking_changes,new_features,deprecations"
)
```

## ⚡ Advanced Features

### Dynamic Token Limits

Each tool has optimized character limits based on typical use cases:

| Tool | Limit | Use Case |
|------|-------|----------|
| `gemini_prompt` | 100K chars | General purpose interactions |
| `gemini_sandbox` | 200K chars | Code execution & development |
| `gemini_eval_plan` | 500K chars | Architecture evaluation |
| `gemini_review_code` | 300K chars | Code review & analysis |
| `gemini_verify_solution` | 800K chars | Complete solution verification |
| `gemini_summarize` | 400K chars | Large content summarization |
| `gemini_summarize_files` | 800K chars | File-based analysis with @filename syntax |
| `gemini_ai_collaboration` | 500K chars | Multi-AI workflow collaboration |
| `gemini_code_review` | 300K chars | Structured code analysis |
| `gemini_extract_structured` | 200K chars | Schema-based data extraction |
| `gemini_git_diff_review` | 150K chars | Git diff analysis |
| `gemini_content_comparison` | 400K chars | Multi-source content comparison |
| **Conversation Tools** | Variable | Context-aware with token management |

### Model Selection

The `model` parameter is accepted on all tools for backward compatibility but is currently **ignored**. Antigravity CLI (`agy`) does not expose a `--model` flag, environment variable, or config key for model selection as of v1.0. The model is determined by your Google account tier (e.g. Gemini 3.5 Flash). A `--model` CLI flag is planned for a future agy release — once available, this server will wire it up automatically.

The server ensures agy always runs with its own isolated backend by unsetting `ANTIGRAVITY_LS_ADDRESS`, preventing interference from any IDE language server running in the same environment.

### Conversation History Management

Stateful multi-turn conversations via agy native `.pb` files:

**Key Features:**
- **Agy-Native Storage**: Conversations stored as protobuf files in `~/.gemini/antigravity-cli/conversations/`
- **JSON Metadata Sidecar**: Title, tags, expiration tracked in `mcp_metadata.json`
- **Automatic Context Building**: Intelligent context assembly respecting token limits
- **Conversation Pruning**: Automatic message and token limit management
- **Configurable Expiration**: Automatic cleanup with customizable retention periods

### Advanced Caching

**TTL-Based Caching**:
- Help/version commands: 30 minutes
- Prompt results: 5 minutes
- Template loading: 30 minutes

**Cache Features**:
- Atomic operations prevent race conditions
- Memory limits prevent unbounded growth
- Automatic cleanup and expiration
- Cache hit/miss metrics tracking

### @filename Syntax Support

23 of the 24 tools support `@filename` syntax for optimal token efficiency:

```python
# Single file
@gemini_prompt("Analyze @config.py")

# Multiple files
@gemini_review_code("@src/auth.py @src/middleware.py")

# Directories and wildcards
@gemini_summarize("@src/ @tests/ @**/*.js")

# Mixed content
@gemini_eval_plan("Based on @requirements.md, implement @design.py")
```

**How it works**: The server parses prompts for `@path` tokens, expands globs, and converts them to `--add-dir` flags for agy. This happens server-side before passing to the CLI.

**Benefits**:
- 50-70% token efficiency improvement
- Direct file reading by agy
- No intermediate processing overhead
- Preserves full context window utilization

### Template System Benefits

The modular template system provides significant advantages for enterprise deployments:

**📈 Maintainability**:

- **Function Size Reduction**: ~70% average reduction in function complexity
- **Separation of Concerns**: Template content isolated from business logic
- **Single Responsibility**: Each template serves a specific AI workflow purpose
- **Version Control**: Template changes tracked independently

**⚡ Performance**:

- **TTL Caching**: 30-minute cache for template loading reduces I/O overhead
- **Memory Efficiency**: Templates loaded once and reused across requests
- **Response Time**: Faster tool execution with cached template access
- **Resource Optimization**: Reduced filesystem access for repeated operations

**🔧 Development Experience**:

- **Modular Architecture**: Each dual-AI workflow tool has dedicated templates
- **Easy Customization**: Templates can be modified without touching core logic
- **Testing**: Templates can be unit tested independently
- **Documentation**: Self-documenting template structure with clear organization

## ⚙️ Configuration

### Environment Variables

The server supports extensive configuration through environment variables:

#### Core Configuration
```bash
export CLI_TIMEOUT=300          # Command timeout (10-3600 seconds)
export CLI_LOG_LEVEL=INFO       # Logging level (DEBUG, INFO, WARNING, ERROR)
export CLI_COMMAND_PATH=agy     # Path to Antigravity CLI executable
export GEMINI_TIMEOUT=300       # Fallback for CLI_TIMEOUT
export GEMINI_LOG_LEVEL=INFO    # Fallback for CLI_LOG_LEVEL
export GEMINI_COMMAND_PATH=agy  # Fallback for CLI_COMMAND_PATH
export CLI_PRINT_TIMEOUT_GRACE=30  # Seconds agy's --print-timeout sits above CLI_TIMEOUT
export CLI_LOG_FILE=            # Optional path for agy diagnostics; keeps stdout clean (do NOT use /dev/null — agy hangs)
```

`--print-timeout`: agy's internal print-mode timeout defaults to 5 minutes. The
server passes `--print-timeout (CLI_TIMEOUT + CLI_PRINT_TIMEOUT_GRACE)s` so that
raising `CLI_TIMEOUT` above 300s no longer lets agy preempt a long run before the
Python-side supervisor timeout fires. The server also sets
`AGY_CLI_HIDE_ACCOUNT_INFO=1` on the agy subprocess so the account/credits header
never leaks into the parsed stdout.

#### Retry Configuration
```bash
export RETRY_MAX_ATTEMPTS=3        # Maximum retry attempts (1-10)
export RETRY_BASE_DELAY=1.0        # Base delay for exponential backoff (0.1-10.0)
export RETRY_MAX_DELAY=30.0        # Maximum delay between retries (5.0-300.0)
```

#### Tool-Specific Limits
```bash
export GEMINI_PROMPT_LIMIT=100000      # gemini_prompt character limit
export GEMINI_SANDBOX_LIMIT=200000     # gemini_sandbox character limit
export GEMINI_EVAL_LIMIT=500000        # gemini_eval_plan character limit
export GEMINI_REVIEW_LIMIT=300000      # gemini_review_code character limit
export GEMINI_VERIFY_LIMIT=800000      # gemini_verify_solution character limit
export GEMINI_SUMMARIZE_LIMIT=400000   # gemini_summarize character limit
export GEMINI_SUMMARIZE_FILES_LIMIT=800000  # gemini_summarize_files character limit
export GEMINI_CONTENT_COMPARISON_LIMIT=400000  # gemini_content_comparison character limit
export GEMINI_AI_COLLABORATION_LIMIT=500000  # gemini_ai_collaboration character limit
export GEMINI_CODE_REVIEW_LIMIT=300000  # gemini_code_review character limit
export GEMINI_EXTRACT_STRUCTURED_LIMIT=200000  # gemini_extract_structured character limit
export GEMINI_GIT_DIFF_REVIEW_LIMIT=150000  # gemini_git_diff_review character limit
```

#### Model Fallback
```bash
export GEMINI_ENABLE_FALLBACK=true     # Enable automatic model fallback
export GEMINI_DEFAULT_MODEL=gemini-2.5-flash      # Default model (accepted but ignored by agy)
export GEMINI_FALLBACK_MODEL=gemini-2.5-flash     # Fallback model (accepted but ignored by agy)
```

#### Rate Limiting
```bash
export GEMINI_RATE_LIMIT_REQUESTS=100  # Requests per time window
export GEMINI_RATE_LIMIT_WINDOW=60     # Time window in seconds
```

#### Conversation Management
```bash
export GEMINI_CONVERSATION_ENABLED="true"          # Enable conversation history
export GEMINI_CONVERSATION_EXPIRATION_HOURS="24"   # Auto-cleanup time
export GEMINI_CONVERSATION_MAX_MESSAGES="10"       # Message history limit
export GEMINI_CONVERSATION_MAX_TOKENS="20000"      # Token history limit
```

#### Enterprise Monitoring (Optional)
```bash
export ENABLE_MONITORING=true          # Master control for all monitoring features
export ENABLE_OPENTELEMETRY=true       # Enable OpenTelemetry distributed tracing
export ENABLE_PROMETHEUS=true          # Enable Prometheus metrics collection
export ENABLE_HEALTH_CHECKS=true       # Enable health check system
export PROMETHEUS_PORT=8000             # Prometheus metrics endpoint port
export OPENTELEMETRY_ENDPOINT="https://otel-collector:4317"  # OpenTelemetry endpoint
export OPENTELEMETRY_SERVICE_NAME="antigravity-cli-mcp-server"    # Service name for tracing
```

#### Security Configuration (Advanced)
```bash
export JSONRPC_MAX_REQUEST_SIZE=1048576     # Max JSON-RPC request size (1MB default)
export JSONRPC_MAX_NESTING_DEPTH=10        # Max object/array nesting depth
export JSONRPC_STRICT_MODE=true            # Enable strict JSON-RPC validation
export GEMINI_SUBPROCESS_MAX_CPU_TIME=300  # Subprocess CPU time limit (seconds)
export GEMINI_SUBPROCESS_MAX_MEMORY_MB=512 # Subprocess memory limit (MB)
```

### Configuration Examples

**Standard Development**:
```bash
# Use defaults - no configuration needed
```

**Full Enterprise Setup**:
```bash
# Core configuration
export CLI_TIMEOUT=600
export GEMINI_EVAL_LIMIT=750000
export GEMINI_REVIEW_LIMIT=600000
export GEMINI_VERIFY_LIMIT=1200000
export RETRY_MAX_ATTEMPTS=5

# Enterprise monitoring
export ENABLE_MONITORING="true"
export ENABLE_PROMETHEUS="true"
export PROMETHEUS_PORT="8000"
```

**High-Performance Setup**:
```bash
export GEMINI_LOG_LEVEL=WARNING
export RETRY_BASE_DELAY=0.5
export RETRY_MAX_DELAY=10.0
export GEMINI_RATE_LIMIT_REQUESTS=500
```

**Debug Configuration**:
```bash
export CLI_LOG_LEVEL=DEBUG
export GEMINI_OUTPUT_FORMAT=json
export CLI_TIMEOUT=120
export ENABLE_STDIN_DEBUG="1"
```

### Response Formats

#### JSON Format (Default)
```json
{
  "status": "success",
  "return_code": 0,
  "stdout": "Response from Gemini AI",
  "stderr": ""
}
```

#### Text Format
```
Response from Gemini AI
```

## 🚀 Performance

### Performance Characteristics

**Operation Times**:
- Fast operations (help, version, metrics): < 2 seconds
- Medium operations (simple prompts): 2-10 seconds
- Complex operations (file analysis, code review): 10-60 seconds
- Large analysis (enterprise codebases): 1-5 minutes
- Conversation context loading: < 1 second

**Concurrency**:
- Async architecture supports 1,000-10,000+ concurrent requests
- Memory-efficient single-threaded design
- Non-blocking I/O operations across all 24 tools

**Memory Usage**:
- Base server: 15-30MB (optimized for enterprise features)
- Per operation: 2-8MB average (varies by tool complexity)
- Bounded caches prevent memory leaks with automatic cleanup

**Cache Effectiveness**:
- Help/version commands: 95-99% hit rate
- Prompt results: 60-80% hit rate for repeated operations
- Template loading: 90-95% hit rate (30-minute TTL)
- Conversation context: 85-95% hit rate

### Performance Optimization

**For High-Throughput Scenarios**:
```bash
export GEMINI_LOG_LEVEL=WARNING      # Reduce logging overhead
export RETRY_BASE_DELAY=0.5          # Faster retry cycles
export GEMINI_RATE_LIMIT_REQUESTS=1000  # Higher rate limits
```

**For Large Content Processing**:
```bash
export CLI_TIMEOUT=1800           # Extended timeout (30 minutes)
export GEMINI_EVAL_LIMIT=1500000     # Maximum evaluation capacity
export GEMINI_VERIFY_LIMIT=2000000   # Maximum verification capacity
```

**For Development Speed**:
```bash
export GEMINI_OUTPUT_FORMAT=text     # Faster response parsing
export RETRY_MAX_ATTEMPTS=1          # Fail fast for debugging
```

### Monitoring

Use the `gemini_metrics` tool to monitor server performance:

```python
@gemini_metrics()
```

**Key Metrics**:
- Commands executed and success rate across all 24 tools
- Average latency and throughput per tool category
- Cache hit rates and effectiveness (3 cache types)
- Error rates and types with detailed classification
- Model usage and fallback statistics
- Memory usage and resource utilization
- Conversation system performance and storage metrics
- Security pattern detection and rate limiting effectiveness

## Testing

### Quick Validation

```bash
# Test imports
python -c "from mcp_server import mcp; print('Server imports OK')"

# Test agy is reachable
python -c "from modules.utils.cli_utils import validate_cli_setup; print(validate_cli_setup())"

# Interactive tool testing via MCP inspector
uv pip install "mcp[dev]"
mcp dev mcp_server.py
```

### Integration Test Suite

The project includes 44 integration tests that validate the MCP server against a real Antigravity CLI installation. Tests cover every `agy` flag/parameter the server uses.

```bash
# Install dev dependencies
uv pip install -r requirements-dev.txt

# Run the full suite (requires agy installed and authenticated)
python -m pytest tests/ -v

# Run only unit/pure tests (no agy API calls, fast)
python -m pytest tests/ -v -k "not prompt and not sandbox and not lifecycle and not continue_with"
```

**Test categories:** CLI setup, prompt execution (`--print`), file context (`--add-dir`), sandbox mode (`--sandbox`), conversation management (`--conversation`, `--continue`), error detection, output sanitization, argument construction, retry logic, MCP tool round-trips, conversation metadata tools.

## 🔧 Troubleshooting

### Common Issues

#### "Tool 'gemini_cli' not found"

**Cause**: MCP client can't connect to server or server isn't running.

**Solutions**:
1. Verify absolute paths in MCP client configuration
2. Check that virtual environment is activated
3. Test server manually: `python mcp_server.py`
4. Check client logs for connection errors

#### "Antigravity CLI not found"

**Cause**: `agy` not installed or not in PATH.

**Solutions**:
1. Install Antigravity CLI: `npm install -g @google-ai/antigravity-cli`
2. Verify installation: `agy --version`
3. Set custom path: `export CLI_COMMAND_PATH=/path/to/agy`

#### Authentication Errors

**Cause**: Gemini API key not configured or invalid.

**Solutions**:
1. Authenticate: `agy login`
2. Verify: `agy --version`
3. Check API quota and billing status

#### Rate Limit Exceeded

**Cause**: Too many requests in short time period.

**Solutions**:
1. Wait for rate limit window to reset
2. Increase limits: `export GEMINI_RATE_LIMIT_REQUESTS=500`
3. Use faster model: Switch to `gemini-2.5-flash`

#### Large Content Failures

**Cause**: Content exceeds tool-specific character limits.

**Solutions**:
1. Check content size: `wc -c your_file.txt`
2. Increase limits: `export GEMINI_EVAL_LIMIT=1000000`
3. Use chunking strategy for very large content
4. Use `gemini_summarize_files` for file-based analysis

#### Server Won't Start

**Diagnostic Steps**:
1. Check Python version: `python --version` (must be 3.10+)
2. Verify dependencies: `pip list | grep mcp`
3. Test imports: `python -c "import mcp"`
4. Check logs: `CLI_LOG_LEVEL=DEBUG python mcp_server.py`

#### Performance Issues

**Optimization Steps**:
1. Monitor metrics: Use `@gemini_metrics()` tool
2. Check cache hit rates (should be >80% for repeated operations)
3. Reduce logging: `export GEMINI_LOG_LEVEL=WARNING`
4. Optimize timeouts: `export CLI_TIMEOUT=120`

### Debug Mode

Enable comprehensive debugging:

```bash
export CLI_LOG_LEVEL=DEBUG
export GEMINI_OUTPUT_FORMAT=json
python mcp_server.py
```

This provides detailed information about:
- Command execution and arguments
- Cache operations and hit rates
- Error details and stack traces
- Performance metrics and timing

### Antigravity CLI Logs

agy writes detailed session logs (including gRPC API calls and model resolution) to:

```
~/.gemini/antigravity-cli/log/cli-YYYYMMDD_HHMMSS.log
```

Check the latest file when debugging issues with agy itself (as opposed to the MCP server layer).

### Getting Help

If you encounter issues not covered here:

1. **Check MCP server logs** for detailed error messages (`CLI_LOG_LEVEL=DEBUG`)
2. **Check agy logs** at `~/.gemini/antigravity-cli/log/` for CLI-level issues
3. **Verify Antigravity CLI** works independently: `agy --print "hello" < /dev/null`
4. **Test with simple commands** first: `@gemini_version()`
5. **Monitor metrics** for performance insights: `@gemini_metrics()`
5. **Check environment variables** for correct configuration

## 📄 Requirements

### System Requirements

- **Python**: 3.10 or higher
- **Operating System**: Linux, macOS, or Windows
- **Memory**: 512MB minimum, 2GB recommended
- **Disk Space**: 100MB for installation
- **Network**: Internet connection for Gemini API access

### Python Dependencies

```
mcp>=0.3.0
cachetools>=5.3.0
```

### Optional Dependencies

```
pytest>=7.0.0        # For development and testing
pytest-mock>=3.10.0  # For mocking in tests
uvicorn[standard]>=0.20.0  # For alternative server deployment
```

### External Dependencies

- **Antigravity CLI**: Google's command-line interface for Gemini AI (`npm install -g @google-ai/antigravity-cli`)
- **Node.js**: Required for Antigravity CLI installation (if using npm)

## Acknowledgments

This project is a fork of [centminmod/gemini-cli-mcp-server](https://github.com/centminmod/gemini-cli-mcp-server). Thanks to the original authors for the Gemini CLI-based MCP server foundation that this project builds upon.
