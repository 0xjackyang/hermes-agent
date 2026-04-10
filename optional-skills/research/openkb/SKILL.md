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

## Runtime Model

- OpenViking owns automatic memory prefetch and writeback
- OpenKB owns compiled wiki truth
- the bridge between them is a command surface, not a second memory provider

The shared helper script lives at:

```bash
$HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py
```

It can run OpenKB locally or over SSH depending on environment variables.

## Configuration

Skill settings live under `skills.config.openkb.*` in `~/.hermes/config.yaml`.

The helper script reads these environment variables at runtime:

- `OPENKB_EXEC_MODE=local|ssh`
- `OPENKB_EXEC_HOST=<ssh-host>` when using SSH mode
- `OPENKB_EXEC_BIN=<openkb command>` defaults to `openkb`
- `OPENKB_EXEC_VENV_ACTIVATE=<path>` optional activate script before calling OpenKB
- `OPENKB_EXEC_KB_HOME=<path>` used for orientation reads
- `OPENKB_ORIENTATION_LOG_LINES=<n>` defaults to `30`

For remote Spark usage, a typical profile `.env` block looks like:

```bash
OPENKB_EXEC_MODE=ssh
OPENKB_EXEC_HOST=spark-jack
OPENKB_EXEC_BIN=openkb
OPENKB_EXEC_VENV_ACTIVATE=~/openkb-runtime/venv/bin/activate
OPENKB_EXEC_KB_HOME=~/openkb-kb
OPENKB_BRIDGE_ENABLED=1
OPENKB_BRIDGE_COMMAND=/path/to/hermes-agent/optional-skills/research/openkb/scripts/openkb_bridge.py
OPENKB_BRIDGE_EXPORT_ENABLED=1
OPENKB_BRIDGE_WRITEBACK_ENABLED=1
OPENKB_BRIDGE_PUBLIC_URL=https://kb.jackyang.com
```

## Session Orientation

Before doing KB work, always orient on the KB:

1. read `SCHEMA.md`
2. read `index.md`
3. read the most recent lines of `log.md`
4. run `openkb verify`

Use:

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py read-orient
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py verify
```

If `openkb verify` fails, stop and repair the KB/runtime before ingesting or
filing content.

## Query Workflow

1. Run:

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py recall --json "<question>"
```

2. Read only the top returned pages.
3. Write the answer yourself.
4. If the answer is durable and grounded in KB sources, file it:

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py file-query \
  --stdin \
  --question "<question>" \
  --sources slug1,slug2
```

Do not ask OpenKB to re-synthesize an answer you already wrote.

## Ingest Workflow

Use one of:

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py ingest-url <url>
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py ingest <path>
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py ingest-scan
```

After explicit ingest, run:

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py maintain --quick
```

## Audit / Ops

```bash
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py verify
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py doctor
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py lint --full
python $HERMES_HOME/skills/research/openkb/scripts/openkb_bridge.py maintain --full
```

## Filing Policy

Only file answers that are:

- durable
- non-trivial
- likely useful again
- grounded in returned KB sources

Do not file transient chat replies or operator noise.
