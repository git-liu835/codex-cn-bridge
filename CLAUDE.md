# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run

```bash
# Install Python backend (editable mode)
pip install -e .

# Start the proxy server
code-cn-bridge start          # production
code-cn-bridge start -v       # debug mode (verbose logging)

# Desktop app (dev mode)
cd desktop && npm install && npm run electron:dev

# Build only
cd desktop && npm run build          # Vite + Electron TypeScript
python scripts/build-backend.py      # PyInstaller → dist-backend/code-cn-bridge.exe

# Full pipeline (Windows)
scripts/build-all.bat

# Docker
docker build -t code-cn-bridge . && docker run -p 8765:8765 code-cn-bridge
```

There are **no tests** in this project.

## Architecture

The proxy sits between Claude Code / Codex CLI and Chinese LLM APIs, translating the OpenAI **Responses API** (used by Claude Code / Codex) to the OpenAI **Chat Completions API** (offered by Qwen, DeepSeek, Kimi, Doubao, GLM).

```
Codex/Claude Code ──POST /v1/responses──> 127.0.0.1:8765 (FastAPI)
  │
  ├── _route_vision()  — detects images, routes to vision-capable model
  ├── translate_request()  — Responses → Chat (protocol.py)
  ├── adapter.preprocess_chat_request()  — provider-specific normalization
  ├── UpstreamClient → Chinese LLM API
  ├── StreamTranslator  — SSE → SSE with reasoning_content filtering
  └── translate_response()  — Chat → Responses (non-streaming)
```

### Key modules

| Module | Role |
|--------|------|
| `server.py` | FastAPI app factory. Routes: `/v1/responses`, `/v1/chat/completions`, `/v1/images/generations`, `/v1/models`, `/health`. Also handles vision routing and image_gen tool interception. |
| `protocol.py` | Bidirectional translation engine. `translate_request()` (Responses→Chat), `translate_response()` (Chat→Responses), `StreamTranslator` (stateful SSE stream converter). |
| `adapters/` | 5 adapters (qwen, deepseek, kimi, doubao, glm) extending `BaseAdapter`. Each handles: field removal, SSE normalization, thinking mode suppression, tool_call extraction from XML/JSON in content. |
| `config.py` | YAML config singleton. Search path: `~/.code-cn-bridge.yaml` → `./config.yaml`. Reads API keys from env vars. `resolve_model(code_name) → (provider, target_model)`. |
| `client.py` | `UpstreamClient` — httpx async HTTP wrapper. Separate clients for streaming (read timeout 600s) vs non-streaming. |
| `admin_api.py` | REST + WebSocket API mounted at `/admin/api` for the desktop UI: model CRUD, settings, logs, config import/export. |
| `middleware.py` | `ErrorHandlingMiddleware`, `RequestLoggingMiddleware`, `ApiKeyFilter` (redacts keys from logs). |
| `stats.py` | Thread-safe `StatsCollector` singleton with rolling 500-entry log buffer and WebSocket push. |

### Desktop app (Electron + React)

- **Main process** (`electron/main.ts`): window management, system tray, spawns Python bridge as child process (auto-restart up to 5 times, 3s delay).
- **Renderer** (`src/`): 5 pages (Dashboard, Models, Settings, Logs, About), 6 themes, i18n (zh/en). Communicates with backend via REST + WebSocket on `localhost:8765/admin/api`.

### Config singleton pattern

`config.py` uses a global `_config_instance`. All modules call `get_config()` instead of passing config around. Hot reload is supported via `POST /admin/reload-config`.

### Model mapping

`config.yaml → model_mapping` maps code model names to provider + target model. Each entry:
```yaml
gpt-5-code:
  provider: deepseek
  target: deepseek-v4-pro
  enabled: true
  is_multimodal: false      # set true if model handles images natively
  vision_alias: null         # fallback vision model when images detected
```

The `resolve_model()` method falls back through: exact match → provider name match → first enabled provider with API key.

## CI / Release

GitHub Actions (`.github/workflows/release.yml`):
- **Trigger:** push any `v*` tag (e.g. `v0.3.0`)
- **Matrix:** `windows-latest`, `macos-latest` (arm64), `ubuntu-latest` — build in parallel
- **Pipeline:** Python backend (PyInstaller) → Electron frontend (Vite + tsc) → electron-builder → upload to GitHub Release

**Critical CI gotchas:**
- Default branch is **`main`** — workflow files MUST be on main or GitHub Actions won't discover them. The `master` branch exists but is secondary.
- **`defaults: run: shell: bash`** is required. Windows runner defaults to PowerShell which breaks `||`, `if`, and other bash syntax.
- **Linux deb requires metadata:** `package.json` must have `author` (with email), `homepage`, and `license` fields, or electron-builder FpmTarget will fail.
- **Linux system deps:** install `libfuse2 dpkg-dev fakeroot` before electron-builder for AppImage + deb targets.
- **macOS is arm64 only:** `macos-latest` is Apple Silicon. Intel Macs need a separate `macos-13` runner.
- **Version comes from `desktop/package.json`**, not the tag.

## Critical gotchas

- **`/v1/models` is required for Codex v0.130+**: Newer Codex starts by probing this endpoint. Without it, Codex gets 404 and silently falls back to the official OpenAI API, bypassing the proxy entirely.
- **Codex v0.130+ default model changed to `gpt-5.5`**, not `gpt-5-code`. Users need a model mapping for `gpt-5.5` in config.
- **Fish shell users (macOS):** fish uses `set -x VAR value` not `export`. Write env vars to `~/.config/fish/config.fish`, never source `~/.zshrc` from fish.
- **reasoning_content filtering**: `StreamTranslator._process_chunk()` (protocol.py:401) explicitly filters `reasoning_content` from DeepSeek to prevent Claude Code from entering reasoning loops. Only `content` deltas are forwarded.
- **thinking mode must be disabled**: DeepSeek, Kimi, and GLM adapters force `thinking: {type: "disabled"}`. Without this, the model may loop indefinitely.
- **image_gen interception**: When `image_gen` appears in tools, `server.py` handles it directly (calls image gen API → downloads → saves to CWD) instead of forwarding to the text LLM.
- **vision routing**: `_has_images()` checks both `content` and `output` fields of input items. When images are detected but no vision model is configured, images are stripped before sending to text models (prevents upstream 400 errors).
- **SSE heartbeat**: `_handle_stream()` sends `: heartbeat\n\n` every 15s during idle periods to keep connections alive through long model reasoning phases.
- **Stream client timeouts**: Streaming uses `httpx.Timeout(connect=30, read=600, write=30, pool=30)`. Non-streaming uses a flat 120s timeout. Both are overridable per-provider via the `timeout` field in config.
- **API key safety**: `ApiKeyFilter` redacts keys from logs. `config.save()` strips env-sourced API keys before writing YAML.
- **No git history safety net**: Always verify changes compile before making further edits. Use `python -m py_compile` for quick syntax checks.

## Version bumping

Bumping requires updating the version in **7 files** (all must match):

| File | Field |
|------|-------|
| `desktop/package.json` | `"version"` |
| `pyproject.toml` | `version` |
| `code_cn_bridge/__init__.py` | `__version__` |
| `code_cn_bridge/cli.py` | `@click.version_option(version=...)` and `click.echo(...)` — 2 places |
| `code_cn_bridge/server.py` | `version="..."` in FastAPI constructor |
| `code_cn_bridge/admin_api.py` | `"version"` in `/admin/api/status` response |

Also update `CHANGELOG.md` with release notes. Then `git tag -a vX.Y.Z` to trigger CI.

## Auto-update

The desktop app uses `electron-updater` with `autoUpdater.autoDownload = false` (user controls each step). The update flow:

```
app starts → setupAutoUpdater()
  ├── getUpdateMirror() reads update_mirror from ~/.code-cn-bridge.yaml
  ├── IF mirror configured:
  │     configureMirror() → fetch latest release tag via mirror API
  │     → autoUpdater.setFeedURL({ provider: 'generic', url: mirrorBaseUrl })
  │     → generic provider downloads latest.yml → downloads installer → verifies SHA512
  └── IF no mirror:
        github provider (from electron-builder.yml publish config)
```

**Mirror mechanism:** The `generic` provider fetches `latest.yml` from the mirror URL, which lists all platform artifacts with SHA512 hashes. electron-updater downloads the installer and verifies the hash.

## CI latest.yml handling

Each platform's `electron-builder` generates its own `latest.yml` (with only its platform's artifacts). These are renamed to `latest-{windows,macos,linux}.yml` before upload to avoid overwriting during artifact merge. The release job then:

1. Downloads all artifacts
2. **Recomputes SHA512 directly from the downloaded files** (do NOT trust the yml's old hash — artifact zip transport can change file sizes)
3. Generates a combined `latest.yml` listing all platform files with correct hashes
4. Deletes the individual platform ymls

## electron-builder.yml critical fields

- **`productName: "code CN Bridge"`** — spaces cause filename mismatches. Explicit `artifactName` is required:
  ```yaml
  win:   { artifactName: "code-CN-Bridge-Setup-${version}.${ext}" }
  mac:   { artifactName: "code-CN-Bridge-${version}.${ext}" }
  linux: { artifactName: "code-CN-Bridge-${version}.${ext}" }
  ```
- **`publish.repo: codex-cn-bridge`** — NOT `code-cn-bridge`. The GitHub repo was renamed but the code project kept the old name.

## System prompt modification warning

Do NOT inject extra system messages or modify the `instructions` field in `translate_request()`. Attempted identity injection — both as separate system message and appended to instructions — caused complete empty output (`response.completed` with `output: []`) from DeepSeek and GLM models. The heartbeat fires every 15s but no content arrives, suggesting the model's safety filters block all responses when the system prompt is tampered with.

## Config persistence

`config.save()` uses **atomic write** (temp file + `Path.replace()`). This prevents data corruption if the process is killed during save (e.g., during auto-update). The atomic rename ensures either the old or new file is intact, never a half-written file.

## Detailed logging

The desktop Logs page has a "detailed logging" toggle. When enabled, `DetailedLoggingMiddleware` (pure ASGI middleware in `middleware.py`) captures full request/response bodies for `/v1/` endpoints:
- Truncated at 8KB
- Auto-cleaned at 100 entries (FIFO)
- Stored in `StatsCollector._detailed_logs` deque
- Exposed via `/admin/api/detailed-logs/*` endpoints
- Renderer displays as expandable formatted JSON cards
