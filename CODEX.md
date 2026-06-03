# CODEX.md

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

# Tests
python test-protocol.py              # unit tests (no API key needed)
python test-protocol.py -v           # verbose output
python test-protocol.py --live       # live tests (requires running bridge + API key)
```

## Architecture

The proxy sits between Claude Code / Codex CLI and Chinese LLM APIs, translating the OpenAI **Responses API** (used by Claude Code / Codex) to the OpenAI **Chat Completions API** (offered by Qwen, DeepSeek, Kimi, Doubao, GLM).

```
Codex/Claude Code ──POST /v1/responses──> 127.0.0.1:8765 (FastAPI)
  │
  ├── _route_vision()  — detects images, routes to vision-capable model
  ├── translate_request()  — Responses → Chat (protocol.py)
  │     ├── previous_response_id / conversation → ResponseCache lookup
  │     ├── reasoning.effort → thinking budget mapping
  │     ├── text.format → response_format (structured output)
  │     └── truncation=auto → _truncate_messages()
  ├── adapter.strip_unsupported()  — graceful degradation
  ├── adapter.preprocess_chat_request()  — provider-specific normalization
  ├── UpstreamClient → Chinese LLM API
  ├── StreamTranslator  — SSE → SSE (reasoning→reasoning items, tool_calls→function_call items)
  │     └── status: "requires_action" when finish_reason="tool_calls"
  └── translate_response()  — Chat → Responses (non-streaming)
      └── response cached via ResponseCache for previous_response_id queries
```

### Key modules

| Module | Role |
|--------|------|
| `server.py` | FastAPI app factory. Routes: `/v1/responses`, `/v1/chat/completions`, `/v1/images/generations`, `/v1/models`, `/health`. Vision routing, image_gen interception, `_handle_stream()` with retry + keepalive, `_close_incomplete_items()`. |
| `protocol.py` | Bidirectional translation engine. `translate_request()` (Responses→Chat), `translate_response()` (Chat→Responses), `StreamTranslator` (stateful SSE→Responses SSE with lazy tool_call events + warmup), `ResponseCache` (LRU for `previous_response_id`), `_truncate_messages()` (token-based with tool pair protection), reasoning_content cache recovery, session affinity TTL cache. |
| `adapters/` | 5 adapters (qwen, deepseek, kimi, doubao, glm) extending `BaseAdapter`. Each has `unsupported_features` set and `strip_unsupported()` for graceful degradation. Also handles: field removal, SSE normalization, thinking mode config, tool_call extraction from XML/JSON in content. |
| `config.py` | YAML config singleton. Search path: `~/.code-cn-bridge.yaml` → `./config.yaml`. New: `verbose_log`, `max_context_tokens`, `response_cache_size`. `resolve_model(code_name) → (provider, target_model)`. |
| `client.py` | `UpstreamClient` — httpx async HTTP wrapper. Separate clients for streaming (read timeout 600s) vs non-streaming (120s). Timeouts overridable per-provider. Multi-key rotation via `rotate_key()`. All clients use `trust_env=False`. |
| `circuit_breaker.py` | ccx-style 3-state circuit breaker per provider: CLOSED→OPEN (3 failures)→HALF_OPEN (30s cooldown probe). Health scoring 0-100. `get_circuit_breaker_registry()` singleton. Integrated in responses endpoint + stream handler. |
| `admin_api.py` | REST + WebSocket API mounted at `/admin/api` for the desktop UI: model CRUD, settings, logs, config import/export. |
| `middleware.py` | `ErrorHandlingMiddleware`, `RequestLoggingMiddleware`, `DetailedLoggingMiddleware` (ASGI-level request/response capture), `ApiKeyFilter`. |
| `stats.py` | Thread-safe `StatsCollector` singleton with rolling 500-entry log buffer, detailed log buffer (100 entries), and WebSocket push. |
| `models.py` | Pydantic models + builder helpers. `ResponsesRequest` defines all v1.0.0 fields: `previous_response_id`, `conversation`, `reasoning`, `text`, `truncation`. |
| `test-protocol.py` | 13 unit tests covering all v1.0.0 protocol features. Run with `python test-protocol.py`. |

### Desktop app (Electron + React)

- **Main process** (`electron/main.ts`): window management, system tray, spawns Python bridge as child process (auto-restart up to 5 times, 3s delay).
- **Renderer** (`src/`): 5 pages (Dashboard, Models, Settings, Logs, About), 6 themes, i18n (zh/en). Communicates with backend via REST + WebSocket on `localhost:8765/admin/api`.

### Config singleton pattern

`config.py` uses a global `_config_instance`. All modules call `get_config()` instead of passing config around. Hot reload is supported via `POST /admin/reload-config`.

### Model mapping

`config.yaml → model_mapping` maps code model names to provider + target model. Each entry:
```yaml
gpt-5.5:
  provider: deepseek
  target: deepseek-v4-pro
  enabled: true
  enable_thinking: true       # enable/disable thinking mode per-model
  thinking_budget: 16384      # thinking token budget (default 4096)
  is_multimodal: false        # set true if model handles images natively
  vision_alias: null          # fallback vision model when images detected
  is_image_gen: false         # set true if this IS an image generation model
  image_gen_alias: null       # fallback image gen model
```

Supports multi-model lists (one alias → multiple backends, only one `enabled: true`):
```yaml
gpt-5.5:
  - provider: deepseek
    target: deepseek-v4-pro
    enabled: true
    enable_thinking: true
  - provider: kimi
    target: kimi-k2.6
    enabled: false
    is_multimodal: true
```

The `resolve_model()` method falls back through: exact match → reverse target match → provider name match → first enabled provider with API key.

### Multi-key provider config

```yaml
providers:
  deepseek:
    adapter: deepseek
    base_url: https://api.deepseek.com
    api_keys:                          # static key list
      - sk-key-1
      - sk-key-2
    # OR: api_keys_env: "DEEPSEEK_KEY_1, DEEPSEEK_KEY_2"  # comma-separated env var names
    # OR: api_key: sk-single-key                            # single key (backward compat)
    # OR: api_key_env: DEEPSEEK_API_KEY                     # single env var (backward compat)
```

### v1.0.0 protocol features

All mappings live in `protocol.py:translate_request()`:

| Responses API field | Chat Completions mapping |
|---------------------|--------------------------|
| `previous_response_id` | `ResponseCache.get_summary()` → injected into system message |
| `conversation.id` | Same cache lookup as above |
| `reasoning: {effort, summary}` | `effort` → `_thinking_budget` (1024/4096/16384); `summary=none` → `_disable_thinking` |
| `text: {format: {type: "json_schema", ...}}` | → `response_format: {type: "json_schema", json_schema: {...}}` |
| `truncation: "auto"` | → `_truncate_messages()` — token estimation + FIFO truncation to fit `max_context_tokens` |
| `tool_choice: "auto"/"none"/"required"/{...}` | Direct passthrough |
| `tools[{type: "image_gen"}]` | → function tool `image_gen` + `_has_image_gen` flag (intercepted in server.py) |

`BaseAdapter` has `unsupported_features: set[str]` and `strip_unsupported()` for graceful degradation — unsupported fields are removed with a warning log before the request reaches the upstream API.

### Response caching

`ResponseCache` (`protocol.py`) — module-level LRU singleton accessed via `get_response_cache()`:
- Stores `{response_id: {id, model, output}}` for the last N responses (default 100, configurable via `response_cache_size`)
- `get_summary(response_id)` extracts text from message/reasoning/function_call items
- Both streaming (`_handle_stream` success) and non-streaming (`/v1/responses` POST) paths store completed responses
- Cache is used by `translate_request()` when `previous_response_id` or `conversation.id` is present
- **Disk persistence**: Responses are saved to `~/.code-cn-bridge/cache/responses/{response_id}.json` (atomic write: temp file + rename). On bridge restart, cache is reloaded from disk sorted by mtime, most recent first. `get()` checks memory first, falls back to disk via `_load_single_from_disk()` which promotes the entry back to memory. Configurable via `response_cache_size` (default 500).

### Project context injection

`config.py:project_context` property auto-injects project rules/memory into every request's `instructions` field. Configure via `~/.code-cn-bridge.yaml`:

```yaml
project_context:
  project_dir: "E:/project/my-app"        # auto-detect CODEX.md/CLAUDE.md/.cursorrules/CODEBUDDY.md/AGENTS.md
  rules_file: "E:/project/my-app/CODEX.md" # explicit rules file path
  project_prompt: "项目简介文本"           # direct text injection
  memory_file: "~/.code-cn-bridge/memory/proj.json"  # persistent cross-session memory
  codex_history: 10                        # read N most recent Codex threads from state_5.sqlite
```

Auto-detection: if `project_dir` or `CODE_PROJECT_DIR` env var is set, scans for CODEX.md → CLAUDE.md → .cursorrules → CODEBUDDY.md → AGENTS.md. The first found file's content is prepended to instructions. `_read_codex_threads()` reads `~/.codex/state_5.sqlite` (threads table: title, first_user_message, preview, cwd, timestamps) and formats them as markdown.

### Thinking mode handling

Each adapter has `supports_thinking_budget: bool`:
- **DeepSeek**: `True` — sends `thinking: {type: "enabled", budget_tokens: N}`. Default budget 16384.
- **All others**: `False` — sends `thinking: {type: "enabled"}` (no budget_tokens). Also floors `max_tokens` to `16384` (Kimi/GLM/Qwen/Doubao) to prevent thinking from exhausting all output tokens.

`enable_thinking: false` in model config sends `thinking: {type: "disabled"}` and removes the max_tokens floor.

### Empty content retry + Budget rectifier (v0.3.22+)

`server.py:_handle_stream()` detects two model failure modes and auto-recovers:

**Empty content retry**: After stream completes, if the response has no tool calls and <50 chars of text content, it's classified as "empty" (common in DeepSeek thinking mode — it thinks then produces nothing). The stream is retried once with `enable_thinking: false`.

**Budget rectifier**: DeepSeek occasionally rejects `budget_tokens` values. The error message is pattern-matched (`_is_budget_constraint_error()`) and params are auto-corrected (`_rectify_budget_params()`: budget → 32000, max_tokens → 64000) before retry.

Both retries count toward `MAX_RETRIES` and trigger key rotation + translator rebuild.

Six features adopted from [BenedictKing/ccx](https://github.com/BenedictKing/ccx) for multi-turn conversation stability:

| Feature | Location | Behavior |
|---------|----------|----------|
| **reasoning_content cache** | `protocol.py` | `save_last_reasoning()` after each response; `_recover_reasoning()` injects cached reasoning into assistant(tool_calls) messages that lost it (DeepSeek known bug). |
| **Circuit breaker** | `circuit_breaker.py` | 3-state per-provider (CLOSED/OPEN/HALF_OPEN). 3 consecutive failures → OPEN (30s cooldown). Health score 0-100. Checked before upstream requests; fast-fails with HTTP 503 when open. |
| **Multi-key rotation** | `client.py`, `config.py` | Provider config supports `api_keys: [k1, k2]` or `api_keys_env: "ENV1,ENV2"`. `UpstreamClient.rotate_key()` on stream retry. `config.get_api_keys()` returns list. |
| **Session affinity** | `protocol.py`, `server.py` | TTL cache (10min) maps `conversation.id` → `provider_name`. `get_affinity()`/`set_affinity()` — same conversation always routes to same provider. |
| **SSE lazy loading** | `protocol.py:StreamTranslator` | Tool call `output_item.added` deferred until first argument delta arrives (not just name), preventing phantom items Codex might wait on. |
| **Response warmup** | `protocol.py:StreamTranslator` | `warmup()` emits `response.created` immediately before upstream request begins, reducing perceived first-byte latency. |

### Key stability interactions

- **Stream retry triggers key rotation**: `_handle_stream()` calls `client.rotate_key()` before rebuilding the translator, so retries use a fresh API key.
- **Circuit breaker gates both stream and non-stream**: `before_request()` check in `responses_endpoint` returns 503 if circuit is OPEN. `on_success()`/`on_failure()` called in both paths.
- **Session affinity + circuit breaker**: Affinity routes to the same provider, but if that provider is OPEN, the 503 response signals Codex to retry. On next attempt without a `conversation.id` match, normal routing picks an available provider.
- **reasoning recovery + session affinity**: Both use the conversation chain. Affinity ensures the same model sees the recovered reasoning.
- **Warmup + lazy loading**: Warmup sends `response.created` eagerly; tool call items arrive only when arguments flow. Together they minimize dead air.

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
- **thinking mode is ENABLED by default**: DeepSeek, Kimi, and GLM adapters set `thinking: {type: "enabled", budget_tokens: 4096}` unless `enable_thinking: false` in model config. Reasoning content is translated to Responses API `reasoning` output items — NOT forwarded as regular content deltas. Set `enable_thinking: false` per-model to disable.
- **reasoning.effort → budget mapping**: `low→1024`, `medium→4096`, `high→16384` tokens. Passed via internal `_thinking_budget` field, consumed by adapter.
- **image_gen interception**: When `image_gen` appears in tools, `server.py` handles it directly (calls image gen API → downloads → saves to CWD) instead of forwarding to the text LLM.
- **vision routing**: `_has_images()` checks both `content` and `output` fields of input items. When images are detected but no vision model is configured, images are stripped before sending to text models (prevents upstream 400 errors).
- **stream stability**: `_handle_stream()` uses 30s chunk timeout (was 10s), sends keepalive after 25s idle, and retries once on disconnection with a fresh `StreamTranslator`. Always emits `response.completed` even on failure via `_close_incomplete_items()`.
- **stream retry gotcha**: When retrying, the translator MUST be rebuilt (`StreamTranslator(model=model)`) or Codex receives corrupted SSE state. The old `_stream_client` must be `aclose()`'d and set to `None` before retry.
- **Stream client timeouts**: Streaming uses `httpx.Timeout(connect=30, read=600, write=30, pool=30)`. Non-streaming uses a flat 120s timeout. Both are overridable per-provider via the `timeout` field in config.
- **API key safety**: `ApiKeyFilter` redacts keys from logs. `config.save()` strips env-sourced API keys before writing YAML.
- **No git history safety net**: Always verify changes compile before making further edits. Use `python -m py_compile` for quick syntax checks.
- **system prompt tampering**: Do NOT inject extra system messages or modify the `instructions` field. Previous attempts caused complete empty output from DeepSeek/GLM models.
- **verbose logging is reset by `create_app()`**: `_setup_logging()` is called twice — first by `cli.py` (with `verbose=True` from `-v`), then by `create_app()` (with `verbose=False`, the default). The second call resets the logger to INFO, so `-v` has NO effect on the request handler's debug output. Workaround: set `verbose_log: true` in config.yaml, which `_setup_logging` checks on line 38 even when `verbose=False`.
- **desktop app auto-restart blocks dev**: The Electron desktop app spawns the bridge as a child process (auto-restart up to 5 times, 3s delay). Multiple desktop instances can keep re-binding port 8765 with the INSTALLED binary, so source-code changes never take effect. When debugging locally, kill ALL `code CN Bridge` Electron processes AND all Python processes before starting the bridge from source. Verify with `netstat -ano | findstr 8765`.
- **stream handler iteration is fragile**: The `_handle_stream()` retry+rebuild pattern (lines 788-844) depends on `asyncio.wait_for(anext(chat_stream))` raising exceptions that are caught and trigger retry. Do NOT switch to `asyncio.wait` + `ensure_future` without understanding that the retry loop relies on the cancelled `anext()` propagating exceptions. Any change to the iteration model breaks the retry contract.
- **`trust_env=False` is required on ALL httpx clients**: System proxy (e.g. Clash/V2Ray on `127.0.0.1:7890`) can intermittently break TLS handshakes to upstream APIs (`BrokenResourceError`). Every `httpx.AsyncClient` in `client.py` and `server.py` MUST have `trust_env=False`. This was the root cause of silent stream disconnections after 5s.
- **Stream retry now rotates API keys**: On retry, `_handle_stream()` calls `client.rotate_key()` before rebuilding the translator. This means the retry uses a different API key if multi-key is configured. Do NOT remove this call without understanding that it enables key-level failover.
- **Circuit breaker is per-provider, not per-request**: The breaker gates ALL requests to a provider. If DeepSeek is OPEN, ALL DeepSeek requests fast-fail with 503. Codex will see the error and may retry. Session affinity can route to a different provider on retry if the conversation.id doesn't match.
- **`enable_thinking: true` + `thinking_budget: 16384` is the new default recommendation for DeepSeek**: The adapter sets `thinking.budget_tokens` and floors `max_tokens` to `budget + 4096` to ensure room for both reasoning and output. Setting `enable_thinking: false` disables the deep thinking capability that makes DeepSeek powerful — only disable if you observe "thinking only, no content" issues.
- **Empty content retry disables thinking**: When the model returns empty/short output (<50 chars, no tool calls), `_handle_stream()` auto-retries with `enable_thinking: false`. This is a fallback, not a fix — if it triggers often, disable thinking for that model in config.
- **Budget rectifier has hardcoded limits**: `_rectify_budget_params()` sets `budget=32000, max_tokens=64000` for DeepSeek. If the model's context window changes, update these constants in `server.py:826-828`.
- **ResponseCache disk path**: `~/.code-cn-bridge/cache/responses/`. Each response is one JSON file. On startup, loads up to `response_cache_size` files sorted by mtime. Old files are cleaned by LRU eviction during `put()`.
- **Project context injection is silent**: If `project_context` is configured but the rules file doesn't exist, it silently skips. Check bridge debug logs to confirm injection.
- **`supports_thinking_budget` is per-adapter**: Only DeepSeek sets it to `True`. Adding a new adapter that supports budget_tokens MUST set this flag, or the thinking block will be sent without budget (causing the model to default to a low value and exhaust thinking tokens).

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
