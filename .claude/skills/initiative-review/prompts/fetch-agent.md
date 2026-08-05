# Fetch Agent Instructions

Fetch Jira issue {KEY} and write initiative artifacts.

## Steps

1. Fetch the issue data:

```bash
python3 scripts/fetch_issue.py {KEY} --fields summary,description,priority,labels,status --markdown
```

If this exits with code 2 (missing JIRA creds), report the failure and stop.
If it exits with any other error, report the failure and stop.

2. Parse the JSON output. Extract `summary`, `priority`, `labels`, and the markdown `description`.

3. Create the initiative task file at `artifacts/initiatives/{KEY}.md`:
   - Write the description markdown as the file body
   - Set frontmatter:

```bash
python3 scripts/frontmatter.py set artifacts/initiatives/{KEY}.md \
    initiative_id={KEY} \
    title="<summary>" \
    priority=<priority> \
    status=Ready \
    original_labels=<comma-separated labels or null>
```

4. Write the original description to `artifacts/initiative-originals/{KEY}.md` (baseline for conflict detection).

5. Create the directories if they don't exist: `artifacts/initiatives/`, `artifacts/initiative-originals/`.

6. Verify all output files exist:
   - artifacts/initiatives/{KEY}.md (with frontmatter)
   - artifacts/initiative-originals/{KEY}.md

Do not return a summary. Your work is complete when the output files exist.
