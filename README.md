# agent-chrome

Run real Google Chrome in isolated, lived-in profiles behind a tiny focus-safe local CDP proxy so AI coding agents can browse logged-in sites without touching your everyday Chrome, triggering remote-debugging approval popups, or stealing OS window focus.

## Why

Connecting agents to your default Chrome profile has three headaches:

1. Chrome remote debugging on a default profile triggers constant approval prompts and shares every cookie and password in your everyday browser.
2. Driving visible Chrome or desktop automation tools steals macOS/desktop focus while you are trying to work.
3. Giving an agent full write access to logged-in social or dashboard sessions is risky when you only want it to read.

`agent-chrome` fixes this by giving each named profile its own `--user-data-dir` under `~/.agent-chrome/profiles/` and putting a lightweight local CDP proxy in front of Chrome:

- **Focus-safe:** rewrites `Target.createTarget` to `background=true`, implements `/json/new` in the background, swallows `Page.bringToFront` and `Target.activateTarget`, and no-ops `/json/activate/<id>`.
- **Optional `no-typing` guard (default):** allows mouse clicks and scrolling so agents can expand threads and navigate UIs, while blocking `Input.dispatchKeyEvent`, `Input.insertText`, and `Input.imeSetComposition`. Use `--mode read-write` when you want typing enabled.
- **Model- and harness-agnostic:** exposes a standard local CDP endpoint (`http://127.0.0.1:9225`) that works with Playwright, Vercel's `agent-browser`, Claude Code, Codex, Gemini CLI, T3 Code, or raw CDP scripts.
- **Concurrent agents:** multiple agents can attach to the same proxy port at once, each opening and closing its own background tab.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (used to run `chrome_proxy.py` and `cdp_browse.py` with inline `aiohttp` metadata, no virtualenv setup needed)
- Google Chrome or Chromium (macOS default path auto-detected; override with `AGENT_CHROME_BIN` if needed)

## Quickstart

```bash
git clone https://github.com/charlesbine/agent-chrome.git
cd agent-chrome
chmod +x agent-chrome chrome_proxy.py cdp_browse.py
ln -sf "$(pwd)/agent-chrome" ~/.local/bin/agent-chrome
```

Create a profile, sign in once visibly, quit Chrome, and start it headless behind the focus-safe proxy:

```bash
# 1. Create a profile (no-typing by default; add --mode read-write for full input)
agent-chrome add research --sites x.com,reddit.com

# 2. Open visible Chrome once to sign in, then quit that window
agent-chrome login research https://x.com/i/flow/login

# 3. Start headless Chrome + focus-safe CDP proxy
agent-chrome start research

# 4. Check running profiles and proxy URLs
agent-chrome list
agent-chrome url research --ensure
# -> http://127.0.0.1:9227
```

## Connecting your tools

Point any CDP-compatible client at the **proxy** URL from `agent-chrome url <profile>` (not the raw upstream Chrome port):

```bash
# Built-in focus-safe background tab helpers (auto-closes its own tab)
agent-chrome read research https://example.com
agent-chrome links research https://example.com --filter "docs"
agent-chrome eval research https://example.com "document.title"
agent-chrome screenshot research https://example.com --out page.png

# Vercel agent-browser
agent-browser --cdp "$(agent-chrome url research --ensure)" snapshot

# Playwright (Python)
# browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9225")
```

## Commands

| Command | Description |
| --- | --- |
| `agent-chrome list` | List profiles, modes, ports, and running state (`--json` supported) |
| `agent-chrome add <name>` | Register a new isolated profile (`--mode no-typing\|read-write`, `--sites`, `--visible`) |
| `agent-chrome login <name> [url]` | Open visible Chrome with that profile to sign in |
| `agent-chrome start <name>` | Start Chrome + focus-safe CDP proxy (`--visible`, `--foreground`) |
| `agent-chrome stop <name>` | Stop Chrome and proxy for that profile |
| `agent-chrome url <name>` | Print the local CDP proxy URL (`--ensure` starts if stopped) |
| `agent-chrome tabs <name>` | List open page tabs (`close-tab <name> <id>` to close one) |
| `agent-chrome read\|links\|eval\|screenshot` | Run one-shot background-tab operations and clean up automatically |

## Running tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
