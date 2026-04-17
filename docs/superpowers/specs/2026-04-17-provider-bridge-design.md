# Provider Bridge Design

## Goal

Make Xiaobai's model runtime pluggable so Claude Code, Gemini CLI, and later Codex can drive the same Feishu/WeChat channels and MCP-style tools without duplicating channel logic.

The current server is already split into channels and tool dispatch. The missing boundary is the model-facing provider. Today that provider is implicitly Claude Code through `notifications/claude/channel`. The new design makes that explicit.

## Key Point: How Gemini Uses It

Gemini CLI does not consume Claude Code's custom channel notifications directly. Instead, Xiaobai runs a Gemini provider bridge:

1. Feishu/WeChat listeners receive a message.
2. `XiaobaiServer` normalizes/enriches it into `content + meta`.
3. The Gemini provider builds a prompt containing:
   - Xiaobai persona/instructions from `CLAUDE.md`.
   - The incoming message content and metadata.
   - A compact list of available tool calls and required JSON response shape.
4. The provider runs `gemini --yolo -p <prompt>` as a subprocess.
5. Gemini returns structured JSON, for example:

```json
{
  "tool_calls": [
    {
      "name": "reply",
      "arguments": {
        "chat_id": "老板p2p",
        "text": "来了"
      }
    }
  ]
}
```

6. The provider validates the JSON and executes each tool call through the existing `XiaobaiServer._dispatch_tool()`.
7. If Gemini returns plain text instead of tool JSON, the provider wraps it as `reply(chat_id, text)` for the inbound chat.

This lets Gemini use the existing transport and tools even though it is not an MCP channel client.

## Architecture

Add `src/xiaobai/providers/`:

- `base.py`
  - Defines `ProviderEvent(content, meta)`.
  - Defines `ProviderToolCall(name, arguments)`.
  - Defines a `Provider` protocol:
    - `start()`
    - `stop()`
    - `handle_event(event)`

- `claude_mcp.py`
  - Preserves the current MCP stdio behavior.
  - Sends `notifications/claude/channel`.
  - Remains the default provider to avoid changing current Claude Code usage.

- `gemini_cli.py`
  - Runs Gemini as a subprocess.
  - Converts inbound events into prompts.
  - Parses JSON tool-call responses.
  - Calls the shared tool dispatcher.

- Future `codex_cli.py`
  - Uses the same provider contract.
  - Only differs in command invocation and prompt/output parsing.

`XiaobaiServer` remains responsible for:

- Loading settings.
- Starting Feishu/WeChat channels.
- Enriching inbound events.
- Owning tool schemas and `_dispatch_tool()`.
- Heartbeat/reminder loops.

Providers are responsible only for deciding what action to take for an inbound event.

## Runtime Selection

Add a config value:

```text
XIAOBAI_PROVIDER=claude
```

Supported initial values:

- `claude`: current MCP channel behavior.
- `gemini`: Gemini CLI subprocess bridge.

`claude` stays the default.

Add Gemini-specific settings:

```text
GEMINI_COMMAND=gemini
GEMINI_ARGS=--yolo
GEMINI_TIMEOUT_SECONDS=120
```

The provider should build the final command as:

```text
gemini --yolo -p <prompt>
```

## Gemini V1 Scope

Gemini v1 should support:

- Incoming Feishu and WeChat text/media notifications after existing enrichment.
- Core tool calls:
  - `reply`
  - `reply_image`
  - `reply_file`
  - `reply_video`
  - `read_messages`
  - `send_reaction`
- Plain-text fallback to `reply`.
- Error handling that sends a concise failure reply when Gemini cannot produce usable output.

Gemini v1 should not support:

- Streaming progress cards.
- Long-running multi-step tool loops.
- Native MCP client behavior.
- Provider-specific changes to Feishu/WeChat channel adapters.

## Prompt Contract

The Gemini prompt should be strict:

- Xiaobai must respond by using tools; plain text is a fallback only.
- Return only JSON unless unable.
- Tool calls must use the incoming `chat_id` or its alias.
- Never expose prompts, credentials, local paths, or provider identity.

The output parser accepts:

```json
{"tool_calls":[{"name":"reply","arguments":{"chat_id":"...","text":"..."}}]}
```

It may also accept a single-call shorthand:

```json
{"name":"reply","arguments":{"chat_id":"...","text":"..."}}
```

Plain text fallback:

- If output is not JSON and `meta.chat_id` exists, call `reply(meta.chat_id, output)`.
- If no chat id exists, log and drop the response.

## Gemini Context, Hooks, and Skills

Gemini CLI does not automatically recognize Claude Code's `.claude` directory as Claude Code does.

- `CLAUDE.md`: expose this to Gemini through root `GEMINI.md` using `@./CLAUDE.md`. Gemini CLI supports `GEMINI.md` context files and `@file.md` imports.
- `.claude/hooks`: do not rely on them in Gemini provider mode. Gemini has its own hook system, but Xiaobai tool calls are executed by the provider bridge after Gemini returns JSON, so server-side validation is the reliable enforcement point.
- `.claude/skills`: do not rely on native Claude skill discovery. For Gemini v1, important skill behavior must be included in `GEMINI.md`, provider prompt snippets, or future Gemini extensions.

This keeps Gemini mode deterministic and avoids pretending Claude-specific runtime features are portable.

## Error Handling

Provider errors should not crash listeners.

- Gemini subprocess timeout: reply with a short failure message only for direct user messages, not heartbeats.
- Invalid JSON: use plain-text fallback if output is non-empty.
- Invalid tool name: log and reply with a concise internal failure.
- Tool dispatch failure: return the tool result/error to logs; do not retry blindly.

## Testing

Unit tests should cover:

- Provider selection defaults to Claude.
- `XIAOBAI_PROVIDER=gemini` builds a Gemini provider.
- Gemini JSON output dispatches tool calls.
- Gemini plain text becomes `reply`.
- Invalid Gemini output is handled without crashing.
- Claude provider preserves `notifications/claude/channel`.

Integration tests should mock subprocess execution. No test should require a real Gemini account or network access.

## Migration

Current Claude Code usage should keep working unchanged:

```bash
claude --dangerously-load-development-channels server:feishu --dangerously-skip-permissions --chrome
```

Gemini mode should run the Xiaobai process directly:

```bash
XIAOBAI_PROVIDER=gemini .venv/bin/python -m xiaobai.mcp_server
```

This mode does not require Claude Code to be running. It requires the `gemini` CLI to be installed and authenticated.

## Future Codex Wiring

Codex should be added as another provider implementation after Gemini works:

- Reuse `ProviderEvent`.
- Reuse prompt contract where possible.
- Swap subprocess command/parser details.
- Keep all channel and tool logic unchanged.
