# OpenViking Memory Provider

Context database by Volcengine (ByteDance) with filesystem-style knowledge hierarchy, tiered retrieval, and automatic memory extraction.

## Requirements

- `pip install openviking`
- OpenViking server running (`openviking-server`)
- Embedding + VLM model configured in `~/.openviking/ov.conf`

## Setup

```bash
hermes memory setup    # select "openviking"
```

Or manually:
```bash
hermes config set memory.provider openviking
echo "OPENVIKING_ENDPOINT=http://localhost:1933" >> ~/.hermes/.env
```

## Config

All config via environment variables in `.env`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENVIKING_ENDPOINT` | `http://127.0.0.1:1933` | Server URL |
| `OPENVIKING_API_KEY` | (none) | API key (optional) |

### Optional OpenKB Bridge

OpenViking stays the only Hermes memory provider. If you also want explicit
OpenKB workflows and selective OpenKB-backed recall/writeback, enable the
bridge command surface:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENKB_BRIDGE_ENABLED` | `0` | Enable OpenKB bridge support inside the OpenViking provider |
| `OPENKB_BRIDGE_COMMAND` | `optional-skills/research/openkb/scripts/openkb_bridge.py` | Command Hermes should call for OpenKB bridge operations |
| `OPENKB_BRIDGE_EXPORT_ENABLED` | `1` | Refresh the OpenKB bridge export before KB recall |
| `OPENKB_BRIDGE_WRITEBACK_ENABLED` | `1` | Mirror durable built-in memory writes into OpenKB |
| `OPENKB_BRIDGE_REFRESH_SECONDS` | `900` | Minimum seconds between bridge export refreshes |
| `OPENKB_BRIDGE_RECALL_LIMIT` | `4` | Maximum OpenKB recall items injected into prompt context |
| `OPENKB_BRIDGE_PUBLIC_URL` | (none) | Public KB URL shown in injected OpenKB context |

The shared helper script can run OpenKB locally or over SSH. It reads:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENKB_EXEC_MODE` | `local` | `local` or `ssh` transport for OpenKB |
| `OPENKB_EXEC_HOST` | (none) | SSH host when `OPENKB_EXEC_MODE=ssh` |
| `OPENKB_EXEC_BIN` | `openkb` | OpenKB executable or wrapper command |
| `OPENKB_EXEC_VENV_ACTIVATE` | (none) | Optional activate script before OpenKB runs |
| `OPENKB_EXEC_KB_HOME` | `~/openkb-kb` | KB path used for orientation reads |
| `OPENKB_ORIENTATION_LOG_LINES` | `30` | Recent `log.md` lines to read during orientation |

This is designed to pair with the optional `openkb` Hermes skill under
`optional-skills/research/openkb/`.

## Tools

| Tool | Description |
|------|-------------|
| `viking_search` | Semantic search with fast/deep/auto modes |
| `viking_read` | Read content at a viking:// URI (abstract/overview/full) |
| `viking_browse` | Filesystem-style navigation (list/tree/stat) |
| `viking_remember` | Store a fact for extraction on session commit |
| `viking_add_resource` | Ingest URLs/docs into the knowledge base |
