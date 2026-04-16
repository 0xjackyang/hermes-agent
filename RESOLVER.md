# Hermes Source Resolver

## Routes
- prompt/system prompt assembly -> `run_agent.py`, `agent/prompt_builder.py`
- profile context and memory injection -> `agent/prompt_builder.py`, `tools/memory_tool.py`, `agent/memory_manager.py`
- skill index / skill loading -> `agent/prompt_builder.py`, `tools/skills_tool.py`
- gateway runtime -> `gateway/`
- config / model / tools -> `hermes_cli/`, `toolsets.py`, `tools/`
- Spark deployment-specific ops -> `/home/jackyujieyang/spark-ops/RESOLVER.md`

## Verify
- `./ops/verify fast --json`
- `./ops/verify standard --json`
- `./ops/verify live --json`
