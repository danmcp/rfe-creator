# Evaluation — rfe.speedrun

Automated evaluation of the `rfe.speedrun` pipeline using the [agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness).

## Quick Start

### Setup

Slash commands below (`/eval-setup`, `/eval-run`, etc.) require the [agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness) to be installed.

```bash
# Install dependencies and configure environment
/eval-setup
```

### Run an evaluation

```bash
# Run with Opus
/eval-run --model opus

# Run with Sonnet (subagents also on Sonnet)
/eval-run --model sonnet --subagent-model sonnet

# Compare against a previous run
/eval-run --model opus --baseline <previous-run-id>

# Run a subset of cases for faster iteration
/eval-run --model opus --case autoscaling
```

### Review results

Each run produces:
- `eval/runs/<run-id>/report.html` — HTML report with scoring summary, per-case details, and baseline diff
- `eval/runs/<run-id>/summary.yaml` — machine-readable scores
- `eval/runs/<run-id>/stdout.log` — full execution trace

## How it works

The evaluation runs the `rfe.speedrun` skill headlessly against 20 test cases derived from real RHAIRFE Jira issues. Each test case provides a problem statement (prompt + clarifying context), and the pipeline creates, reviews, auto-fixes, and (dry-run) submits RFEs.

### How it was generated

`/eval-analyze --skill rfe.speedrun` recursively read the skill chain (rfe.speedrun -> rfe.create, rfe.auto-fix, rfe.review, rfe.split, rfe.submit, assess-rfe) and generated `eval.yaml` with dataset schema, output descriptions, and suggested judges. The configuration and judges were then iteratively refined through multiple eval runs across Opus and Sonnet.

### Configuration

- **`eval.yaml`** — defines the skill, dataset, outputs, judges, and thresholds.
- **`eval.md`** — cached skill analysis (auto-generated, tracks SKILL.md hash for freshness).
- **`eval/config/pairwise-judge.md`** — prompt for blind A/B comparison across runs.

### Dataset

`eval/dataset/cases/` contains 20 test cases, each with:

| File | Purpose |
|------|---------|
| `input.yaml` | Skill input: `prompt`, `priority`, `clarifying_context` |
| `annotations.yaml` | Expected scores, feasibility/recommendation expectations, test tags |

Input files contain only the fields the skill needs in batch Mode A — no Jira keys or existing RFE content.

### Judges

| Judge | Type | What it checks |
|-------|------|----------------|
| `files_exist` | check | Task + review files produced |
| `frontmatter_valid` | check | YAML schema, score ranges, pass logic consistency |
| `run_report_exists` | check | Auto-fix YAML run report with required fields |
| `recommendation_consistency` | check | pass/fail aligns with recommendation, infeasible != submit |
| `pipeline_flow` | check | Phases ran, no tracebacks, no Phase 1 deletions |
| `architecture_context_used` | check | Feasibility files must not indicate missing architecture context |
| `rfe_quality` | LLM | RFE quality (WHAT/WHY/HOW/task/scope) + calibration accuracy |
| `revision_quality` | LLM | Revision improvement + content preservation |
| `pairwise` | LLM | Blind A/B comparison (only with `--baseline`) |

### Tool interception

During evaluation, PreToolUse hooks:
- **Auto-answer** `AskUserQuestion` prompts from test case context
- **Block Jira** interactions (the skill runs with `--dry-run`)

---

# Evaluation — initiative-speedrun

Automated evaluation of the `initiative-speedrun` pipeline using the [agent-eval-harness](https://github.com/opendatahub-io/agent-eval-harness).

## Quick Start

```bash
# Run with Opus
/eval-run --config eval-initiative.yaml --model opus

# Compare against a previous run
/eval-run --config eval-initiative.yaml --model opus --baseline <previous-run-id>
```

## How it works

The evaluation runs the `initiative-speedrun` skill headlessly against 16 test cases derived from real RHOAIENG Jira initiatives. Each test case provides an objective (prompt + clarifying context), and the pipeline creates, reviews (with assessment, feasibility, and strategic alignment), auto-fixes, and (dry-run) submits Initiatives.

### Configuration

- **`eval-initiative.yaml`** — defines the skill, dataset, outputs, judges, and thresholds.
- **`eval/config/initiative-pairwise-judge.md`** — prompt for blind A/B comparison across runs.

### Dataset

`eval/initiative-dataset/cases/` contains 16 test cases, each with:

| File | Purpose |
|------|---------|
| `input.yaml` | Skill input: `prompt`, `priority`, `clarifying_context` |
| `annotations.yaml` | Expected scores, feasibility/recommendation/alignment expectations, test tags |

One case (`case-012`, tagged `sparse-input`) provides minimal context to test sparse-input handling.

### Judges

| Judge | Type | What it checks |
|-------|------|----------------|
| `files_exist` | check | Task + review files produced |
| `frontmatter_valid` | check | YAML schema, score ranges, alignment enum, pass logic consistency |
| `run_report_exists` | check | Initiative run report YAML with required fields |
| `recommendation_consistency` | check | pass/fail aligns with recommendation, infeasible != submit, weak alignment sets needs_attention |
| `pipeline_flow` | check | Phases ran, no fatal tracebacks |
| `architecture_context_used` | check | Feasibility files used architecture context |
| `initiative_quality` | LLM | Initiative quality (WHAT/WHY/Scope/HOW/Right-sized) + calibration accuracy |
| `revision_quality` | LLM | Revision improvement + content preservation |
| `pairwise` | LLM | Blind A/B comparison (only with `--baseline`) |
