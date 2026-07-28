---
name: initiative.review
description: Review and improve Initiatives. Accepts one or more Jira keys (e.g., /initiative.review RHOAIENG-12345) to fetch and review existing Initiatives, or reviews local artifacts from /initiative.create.
user-invocable: true
allowed-tools: Glob, Bash, Agent, AskUserQuestion
---

You are an Initiative review orchestrator. Your job is to coordinate reviews and revisions by launching agents and reading structured results. **Critical: never read file contents into your context — only read frontmatter via `scripts/frontmatter.py read` and check file existence via Glob.** All content-heavy work is delegated to agents.

## Review Step 0: Parse Arguments and Persist Flags

Parse `$ARGUMENTS` for flags and IDs:
- Strip `--headless` flag if present (suppresses end-of-run summary)
- Strip `--caller <value>` flag if present (e.g. `--caller split` — indicates this review was invoked by another skill)
- Remaining arguments are one or more space-separated Initiative IDs (RHOAIENG-NNNN or INIT-NNN)

Persist parsed flags:

```bash
python3 scripts/state.py init tmp/initiative-review-config.yaml headless=<true/false> caller=<value_or_none>
```

Persist all IDs to disk:

```bash
python3 scripts/state.py write-ids tmp/initiative-review-all-ids.txt <all_IDs>
```

For each ID, check if `artifacts/initiatives/<id>.md` already exists locally (use Glob, don't read the file). Separate IDs into:
- **Local**: task file exists — skip fetch
- **Remote**: task file missing — needs Jira fetch

## Review Step 1: Fetch Missing Initiatives

For each remote ID, launch a **fetch agent** (run_in_background: true):

```
Read .claude/skills/initiative.review/prompts/fetch-agent.md and follow all instructions. Substitute {KEY} with <ID> throughout.
```

Wait for all fetch agents to complete. Verify task files exist via Glob. For any missing, write an error to the review file:

```bash
python3 scripts/frontmatter.py set artifacts/initiative-reviews/<ID>-review.md initiative_id=<ID> pass=false recommendation=revise feasibility=feasible auto_revised=false needs_attention=true error="fetch_failed: task file not created"
```

Remove failed IDs from the processing list and continue with remaining IDs.

## Review Step 1.5: Setup

Run these in parallel (two Bash calls):

```bash
bash scripts/fetch-architecture-context.sh
```

```bash
bash scripts/bootstrap-assess-rfe.sh
```

If architecture fetch fails, proceed without it. If bootstrap fails, note it — review agents will do basic quality checks instead.

## Review Step 2: Launch Assessment + Feasibility Agents

For each ID being reviewed:

**Prepare assessment:**

```bash
python3 scripts/prep_assess.py <ID>
```

**Launch assess agent** (run_in_background: true, subagent_type: initiative-scorer):

```
Read .claude/skills/initiative.review/prompts/assess-agent.md and follow all instructions. Substitute: {KEY}=<ID>, {DATA_FILE}=tmp/rfe-assess/single/<ID>.md, {RUN_DIR}=tmp/rfe-assess/single, {PROMPT_PATH}=.context/assess-rfe/skills/assess-initiative/scripts/agent_prompt.md
```

**Launch feasibility agent** (run_in_background: true) — one per ID:

```
Read the skill file at .claude/skills/initiative-feasibility-review/SKILL.md and follow all instructions in the body (everything after the YAML frontmatter). The Initiative ID to review is: <ID>
```

**Launch alignment agent** (run_in_background: true) — one per ID that has a RHAISTRAT parent:

Read the parent_key from the Initiative's frontmatter output above. If the parent_key matches `RHAISTRAT-*`, launch:

```
Read the skill file at .claude/skills/strategic-alignment-review/SKILL.md and follow all instructions in the body (everything after the YAML frontmatter). The Initiative ID to review is: <ID>
```

If no RHAISTRAT parent_key, skip the alignment agent for this ID.

Launch all agents for all IDs in parallel (up to 3N agents total for N IDs: assess + feasibility + alignment).

Wait for all to complete. After completion, check prerequisites for each ID via Glob:
- If assess result (`tmp/rfe-assess/single/<ID>.result.md`) is missing → write error: `assess_failed`
- If feasibility file (`artifacts/initiative-reviews/<ID>-feasibility.md`) is missing → write error: `feasibility_failed`
- If alignment file (`artifacts/initiative-reviews/<ID>-alignment.md`) is missing AND a RHAISTRAT parent_key exists → note but do not treat as a blocking error (alignment is informational)

For any missing prerequisite:

```bash
python3 scripts/frontmatter.py set artifacts/initiative-reviews/<ID>-review.md initiative_id=<ID> pass=false recommendation=revise feasibility=feasible auto_revised=false needs_attention=true error="<assess_failed or feasibility_failed>: file not created"
```

Remove failed IDs from the processing list and continue with remaining IDs.

## Review Step 3: Launch Review Agents

For each remaining ID, launch a **review agent** (run_in_background: true):

```
Read .claude/skills/initiative.review/prompts/review-agent.md and follow all instructions. Substitute: {ID}=<ID>, {ASSESS_PATH}=tmp/rfe-assess/single/<ID>.result.md, {FEASIBILITY_PATH}=artifacts/initiative-reviews/<ID>-feasibility.md, {ALIGNMENT_PATH}=artifacts/initiative-reviews/<ID>-alignment.md, {FIRST_PASS}=true
```

Launch all review agents in parallel.

Wait for all to complete. For any ID where the review file is missing or has no frontmatter, write error: `review_failed`.

## Review Step 4: Launch Revise Agents

After all review agents complete, re-read the ID list from disk:

```bash
python3 scripts/state.py read-ids tmp/initiative-review-all-ids.txt
```

Determine which IDs need revision:

```bash
python3 scripts/filter_for_revision.py <all_IDs_from_file>
```

The script outputs the IDs that need revision (filters out passing, infeasible, and rejected IDs). If the output is empty, skip to Review Step 5.

For each ID returned, launch a **revise agent** (run_in_background: true):

```
Read .claude/skills/initiative.review/prompts/revise-agent.md and follow all instructions. Substitute {ID} with <ID> throughout.
```

Launch all revise agents in parallel. Wait for all to complete.

**Post-processing: fix auto_revised flag.** The revise agent may run out of budget before setting `auto_revised=true`. After all agents complete, run the batch check which compares originals to task files and sets the flag directly in review frontmatter:

```bash
python3 scripts/check_revised.py --type initiative --batch <revised_IDs>
```

## Review Step 4a: Reassess Revised Initiatives

After all revise agents complete, re-read the IDs that were revised:

```bash
python3 scripts/filter_for_revision.py <all_IDs_from_file>
```

Wait — filter_for_revision won't return already-revised IDs. Instead, collect the IDs that had revise agents launched (from Step 4). If none were revised, skip to Step 5.

For revised IDs, persist them:

```bash
python3 scripts/state.py write-ids tmp/initiative-review-reassess-ids.txt <revised_IDs>
```

**4a-1. Save cumulative state and remove review files:**

```bash
python3 scripts/preserve_review_state.py save <all_reassess_IDs>
```

For each reassess ID, remove old review and assess files:

```bash
rm artifacts/initiative-reviews/<ID>-review.md
rm tmp/rfe-assess/single/<ID>.result.md
```

**4a-2. Re-run assessment.** For each reassess ID:

```bash
python3 scripts/prep_assess.py <ID>
```

Launch an **assess agent** (run_in_background: true, subagent_type: initiative-scorer) for each reassess ID:

```
Read .claude/skills/initiative.review/prompts/assess-agent.md and follow all instructions. Substitute: {KEY}=<ID>, {DATA_FILE}=tmp/rfe-assess/single/<ID>.md, {RUN_DIR}=tmp/rfe-assess/single, {PROMPT_PATH}=.context/assess-rfe/skills/assess-initiative/scripts/agent_prompt.md
```

Launch all assess agents in parallel. Wait for all to complete.

**4a-3. Launch review agents.** Re-read reassess IDs from disk:

```bash
python3 scripts/state.py read-ids tmp/initiative-review-reassess-ids.txt
```

For each reassess ID, launch a **review agent** (run_in_background: true):

```
Read .claude/skills/initiative.review/prompts/review-agent.md and follow all instructions. Substitute: {ID}=<ID>, {ASSESS_PATH}=tmp/rfe-assess/single/<ID>.result.md, {FEASIBILITY_PATH}=artifacts/initiative-reviews/<ID>-feasibility.md, {ALIGNMENT_PATH}=artifacts/initiative-reviews/<ID>-alignment.md, {FIRST_PASS}=false
```

Launch all review agents in parallel. Wait for all to complete.

**4a-4. Restore before_scores.** Re-read reassess IDs from disk:

```bash
python3 scripts/state.py read-ids tmp/initiative-review-reassess-ids.txt
```

```bash
python3 scripts/preserve_review_state.py restore <all_reassess_IDs_from_file>
```

## Review Step 5: Finalize

Re-read flags:

```bash
python3 scripts/state.py read tmp/initiative-review-config.yaml
```

Re-read ID list:

```bash
python3 scripts/state.py read-ids tmp/initiative-review-all-ids.txt
```

**If `caller: split`**: Output the text "initiative.review step completed." and stop. (Split orchestrator handles the summary.)

**If `headless: true`**: Output the text "initiative.review step completed." and stop.

**If interactive**: For each ID, read the review frontmatter and present a summary:

- **All pass**: Tell the user Initiatives are ready for `/initiative.submit`.
- **Some revised**: Report which Initiatives were auto-revised and their current status.
- **Some need attention**: List the Initiatives that need human review, with reasons.
- **Some rejected**: List rejected Initiatives and suggest the user edit and re-run `/initiative.review`.
- **Errors**: Report which IDs had errors and suggest retrying.

$ARGUMENTS
