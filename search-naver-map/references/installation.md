# Installation

## Agent-managed installation

Give the agent the repository URL and ask it to install `search-naver-map`, prepare the isolated runtime, and verify capability discovery.

The required verification is:

```bash
cd "<installed search-naver-map directory>"
python3 scripts/bootstrap.py
bin/naver-place capabilities --json
```

## Manual installation

Copy or symlink the `search-naver-map` directory into the skill directory used by the agent, then run the same bootstrap command.

Initially supported layouts:

- Codex: `$CODEX_HOME/skills/search-naver-map` or `~/.codex/skills/search-naver-map`
- Claude Code: `~/.claude/skills/search-naver-map`
- Agent Skills convention: `~/.agents/skills/search-naver-map`

Bootstrap requires Python 3.10 or newer. It creates `.venv`, installs the pinned runtime dependency, and performs a capability-discovery smoke test. Use `python3 scripts/bootstrap.py --dev` to install the test runner.

Bootstrap is idempotent. Do not replace it with a global `pip install`.
