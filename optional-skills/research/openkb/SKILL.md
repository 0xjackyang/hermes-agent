---
name: openkb
description: "Use OpenKB as a compiled markdown knowledge base. Orient on schema/index/log, query structured recall, ingest sources, file durable answers, and maintain the KB while OpenViking remains the sole memory provider."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [knowledge-base, wiki, research, markdown, openviking]
    category: research
    related_skills: [llm-wiki, qmd]
    config:
      - key: openkb.bin
        description: OpenKB executable or wrapper command
        default: openkb
        prompt: OpenKB command
      - key: openkb.kb_path
        description: Path to the OpenKB repository for orientation reads
        default: ~/openkb-kb
        prompt: OpenKB KB path
      - key: openkb.public_url
        description: Public URL for the KB, if one exists
        default: ""
        prompt: OpenKB public URL (optional)
      - key: openkb.orientation_log_lines
        description: Number of recent log lines to read during orientation
        default: "30"
        prompt: OpenKB orientation log lines
      - key: openkb.auto_file_queries
        description: Whether durable Hermes answers should be filed back into OpenKB
        default: "true"
        prompt: Auto-file durable query answers
      - key: openkb.file_query_min_chars
        description: Minimum answer length before auto-filing is considered
        default: "1200"
        prompt: Minimum filed answer length
---

# OpenKB

OpenKB is a compiled markdown knowledge base with explicit ingest, query,
maintenance, and publication workflows.

Use this skill when the user wants to:

- query or search their KB/wiki
- ingest articles, docs, or notes into the KB
- file a durable answer back into the KB
- verify, lint, or maintain the KB

OpenViking remains the only memory provider. This skill is the explicit KB
workflow surface.

For normal KB operations, **prefer the configured OpenKB CLI directly**.
Do not search the repo for helper scripts when the KB is already locally
reachable through `skills.config.openkb.bin`.

## Runtime Model

- OpenViking owns automatic memory prefetch and writeback
- OpenKB owns compiled wiki truth
- the bridge between them is a command surface, not a second memory provider

The shared helper script lives at:

```bash
$HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py
```

It can run OpenKB locally or over SSH depending on environment variables.
This helper is primarily for bridge/runtime plumbing. For explicit skill
workflows, use the configured OpenKB CLI directly unless the profile is
explicitly set up for remote-only access.

## Configuration

Skill settings live under `skills.config.openkb.*` in `~/.hermes/config.yaml`.

The helper script reads these environment variables at runtime:

- `OPENKB_EXEC_MODE=local|ssh`
- `OPENKB_EXEC_HOST=<ssh-host>` when using SSH mode
- `OPENKB_EXEC_BIN=<openkb command>` defaults to `openkb`
- `OPENKB_EXEC_VENV_ACTIVATE=<path>` optional activate script before calling OpenKB
- `OPENKB_EXEC_KB_HOME=<path>` used for orientation reads
- `OPENKB_ORIENTATION_LOG_LINES=<n>` defaults to `30`

For a remote OpenKB host, a typical profile `.env` block looks like:

```bash
OPENKB_EXEC_MODE=ssh
OPENKB_EXEC_HOST=your-openkb-host
OPENKB_EXEC_BIN=openkb
OPENKB_EXEC_VENV_ACTIVATE=~/openkb-runtime/venv/bin/activate   # if needed
OPENKB_EXEC_KB_HOME=~/openkb-kb
OPENKB_BRIDGE_ENABLED=1
OPENKB_BRIDGE_COMMAND=/path/to/hermes-agent/optional-skills/research/openkb/scripts/openkb_bridge.py
OPENKB_BRIDGE_EXPORT_ENABLED=1
OPENKB_BRIDGE_WRITEBACK_ENABLED=1
OPENKB_BRIDGE_PUBLIC_URL=https://kb.example.com
```

For a local OpenKB runtime, use the configured CLI directly:

```bash
openkb verify
openkb recall --json "your topic"
```

## Session Orientation

For **query-only lookups**, start with `openkb recall --json` first. Do not
read `SCHEMA.md`, `index.md`, or `log.md` unless:

- recall results are ambiguous
- you are about to ingest or file content
- you are about to modify or maintain the KB

For ingest, filing, maintenance, or broader KB work, orient first:

1. read `SCHEMA.md`
2. read `index.md`
3. read the most recent lines of `log.md`
4. run `openkb verify`

If OpenKB is local, use the configured KB path and CLI directly:

```bash
sed -n '1,220p' "$OPENKB_EXEC_KB_HOME/SCHEMA.md"
sed -n '1,220p' "$OPENKB_EXEC_KB_HOME/index.md"
tail -n 30 "$OPENKB_EXEC_KB_HOME/log.md"
openkb verify
```

Only use the helper wrapper if the profile is explicitly using a remote/SSH
OpenKB transport.

Helper form:

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py read-orient
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py verify
```

If `openkb verify` fails, stop and repair the KB/runtime before ingesting or
filing content.

## Query Workflow

1. Run the configured OpenKB CLI directly. For simple answer-only questions,
   this is the first step.

```bash
openkb recall --json "<question>"
```

2. Read only the top returned pages.
3. Write the answer yourself.
4. If the answer is durable and grounded in KB sources, file it:

```bash
openkb file-query \
  --stdin \
  --question "<question>" \
  --sources slug1,slug2
```

Do not ask OpenKB to re-synthesize an answer you already wrote.

## Ingest Workflow

Use one of:

```bash
openkb ingest --url <url>
openkb ingest <path>
openkb ingest --scan
```

After explicit ingest, run:

```bash
openkb maintain --quick
```

## Audit / Ops

```bash
openkb verify
openkb doctor
openkb lint --full
openkb maintain --full
```

## Filing Policy

Only file answers that are:

- durable
- non-trivial
- likely useful again
- grounded in returned KB sources

Do not file transient chat replies or operator noise.
