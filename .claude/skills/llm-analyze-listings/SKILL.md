---
name: llm-analyze-listings
description: |
  Use this skill to run a full LLM enrichment pass over the latest job-market
  snapshot WITHOUT spawning claude.exe subprocesses (which pop console windows
  on Windows). Dispatches parallel subagents instead. Trigger when the user
  asks to "analyze listings via subagents", "enrich the snapshot", "run the
  LLM analyze pass", or any variation that implies extracting certifications /
  YoE / degree / clearance from the filtered listings using the LLM.

  Do NOT use this for the regex filtering pass — that's still
  `python -m job_market_intel --reclassify ...` (no LLM, no windows).
---

# Run LLM enrichment via parallel subagents

This skill orchestrates the analyze step (Step 3 of the user's
regex → LLM-filter → LLM-analyze pipeline) using the Agent tool, avoiding the
window-pop issue caused by `claude.exe` subprocesses on Windows.

## When to invoke

Run when the user wants the certs / YoE / degree / clearance fields filled in
on the surviving post-filter listings. Should be invoked AFTER a regular
`--reclassify` run (with or without `--llm-backend cli`) has produced
`reports/snapshot-<date>.json`.

## Workflow

### 1. Prepare chunks

Run the prep CLI (no LLM call, fast, no windows):

```bash
python -m job_market_intel \
  --subagent-prepare reports/snapshot-<date>.json \
  --subagent-dir cache/subagent-chunks \
  --subagent-chunk-size 50
```

This writes:
- `cache/subagent-chunks/chunk-001.json` ... `chunk-NNN.json` (each ~50 listings)
- `cache/subagent-chunks/manifest.json` — contains all chunk paths AND the prompt template to give each agent.

Read `manifest.json` to get the list of chunk files and the prompt template.

### 2. Dispatch one subagent per chunk, in PARALLEL

For each chunk file in the manifest, launch a `general-purpose` subagent via the Agent tool.
Send ALL agent invocations in a SINGLE message so they run in parallel.

For each agent:
- `subagent_type`: `general-purpose`
- `description`: `Enrich job listings chunk N` (short)
- `prompt`: build it from the manifest's `prompt_template`, substituting:
  - `{input_path}` → the chunk file path (e.g. `cache/subagent-chunks/chunk-001.json`)
  - `{output_path}` → `cache/subagent-chunks/chunk-001-result.json` (same number, with `-result` suffix)

The agent will:
1. Use the Read tool to load the chunk JSON.
2. Process each listing, extracting structured fields per the schema.
3. Use the Write tool to write the result JSON array to the output path.

### 3. Merge results back into a snapshot

After ALL agents complete, run the merge CLI:

```bash
python -m job_market_intel \
  --subagent-merge reports/snapshot-<date>.json \
  --subagent-dir cache/subagent-chunks \
  --output-dir reports \
  --today <date>
```

This reads every `chunk-NNN-result.json`, merges per-listing extractions into
the snapshot using the same union/fill-only-if-blank rules as the
ClaudeCliExtractor, and writes the fresh `snapshot-<date>.json`,
`snapshot-<date>.csv`, `report-<date>.md`, and updates `trend.csv`.

### 4. Report numbers to the user

After merge completes, summarize:
- Total listings processed
- How many got LLM enrichments (should be ~100%)
- Coverage delta (% with certs / YoE / degree before vs after)
- A specific spot-check (e.g., the Verizon SOC Analyst listing — show what was extracted)

## Failure modes & recovery

- **Agent returns prose instead of JSON to the file**: re-dispatch that single chunk with a sharper "JSON ONLY" reminder in the prompt.
- **Result file missing for a chunk**: `--subagent-merge` will report `WARN: N listings had no agent result`. Those listings keep their pre-enrichment extracted state. Re-dispatch the missing chunks and re-merge.
- **JSON parse error in a result file**: open the file, look at what the agent wrote, fix it manually (or re-dispatch).

## Why this exists

The standard `--enrich-filtered` mode spawns `claude.exe` once per batch via
Python subprocess. On Windows, each spawn pops up a console window (`claude.exe`
internally spawns helper processes that allocate their own consoles). For
~400 listings with batch_size=20, that's 20+ window flashes — unacceptable
for interactive work.

Subagents run inside the parent Claude Code session — no subprocesses, no
windows, parallel by default. Same merge logic, same output schema, same
result.
