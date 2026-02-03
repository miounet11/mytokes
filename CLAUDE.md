# AI History Manager - Claude Code Context

## Project Overview

This is an **AI API proxy service** that sits between Claude Code CLI and the backend AI API (Kiro/AWS Bedrock).

**Key Functions:**
1. Anthropic API format conversion (Anthropic ↔ OpenAI)
2. Intelligent history message management (truncation/summarization)
3. Tool call parsing and conversion (**Native OpenAI tools support**)
4. **Smart model routing** (Opus ↔ Sonnet)

---

## Critical Configuration Files

| File | Purpose |
|------|---------|
| `api_server.py` | Main service file with all configurations |
| `start.sh` | Service startup script |
| `/var/log/ai-history-manager.log` | Service logs |

---

## Service Management

### Start Service
```bash
cd /www/wwwroot/ai-history-manager
bash start.sh
```

### Stop Service
```bash
pkill -9 -f "uvicorn api_server:app"
```

### Restart Service
```bash
bash start.sh  # Automatically stops old process first
```

### Check Status
```bash
# Check if running
pgrep -f "uvicorn api_server:app"

# Health check
curl http://127.0.0.1:8100/

# View logs
tail -f /var/log/ai-history-manager.log

# Check routing stats
curl http://127.0.0.1:8100/admin/routing/stats
```

### Common Startup Issues

| Error | Cause | Solution |
|-------|-------|----------|
| `Address already in use` | Port 8100 occupied | `pkill -9 -f "uvicorn api_server:app" && sleep 2 && bash start.sh` |
| Health check failed | Service starting | Wait 3-5 seconds and check logs |
| Import error | Missing dependency | `pip install httpx fastapi uvicorn uvloop httptools` |

---

## Native Tools Support (NEW)

Located in `api_server.py` around line 190-200.

### Configuration

```python
# Enable native OpenAI tools format (recommended)
NATIVE_TOOLS_ENABLED = True

# Enable fallback to text injection when native tools fail
NATIVE_TOOLS_FALLBACK_ENABLED = True
```

### Environment Variables

```bash
# Enable/disable native tools (default: true)
export NATIVE_TOOLS_ENABLED=true

# Enable/disable fallback (default: true)
export NATIVE_TOOLS_FALLBACK_ENABLED=true
```

### How It Works

**Native Tools Mode (Default):**
- Tools are passed directly to Kiro gateway as OpenAI `tools` parameter
- Kiro returns structured `tool_calls` in response
- Benefits: Lower token usage, more accurate parsing, parallel tool calls

**Fallback Mode (Legacy):**
- Tools are injected into system prompt as text instructions
- Model outputs `[Calling tool: xxx]` format
- Proxy parses text to extract tool calls

### Logs

```
[request_id] Anthropic -> OpenAI: model=xxx, tools=5(原生)
[request_id] 检测到原生 tool_calls: 2 个
```

---

## Async Summary Optimization (NEW)

Located in `api_server.py` around line 165-190.

### The Problem

Previously, when messages exceeded the threshold:
1. Synchronous summary generation: ~10s blocking
2. Synchronous context extraction: ~3-5s blocking
3. **Total first-token delay: ~15-17s**

### The Solution

**Async Summary Mode** - Non-blocking summary generation:

```
Request arrives → Check cache → Cache hit? → Use cached → Send immediately
                              ↓
                           Cache miss
                              ↓
                    Use simple truncation → Send immediately
                              ↓
                    Background: Generate summary → Update cache
```

### Configuration

```python
ASYNC_SUMMARY_CONFIG = {
    "enabled": True,              # Enable async mode
    "fast_first_request": True,   # First request uses simple truncation
    "max_pending_tasks": 100,     # Max background tasks
    "update_interval_messages": 5, # Update summary every N new messages
    "task_timeout": 30,           # Background task timeout (seconds)
}
```

### Environment Variables

```bash
export ASYNC_SUMMARY_ENABLED=true
export ASYNC_SUMMARY_FAST_FIRST=true
export ASYNC_SUMMARY_UPDATE_INTERVAL=5
```

### Monitor

```bash
# Check async summary stats
curl http://127.0.0.1:8100/admin/async-summary/stats
```

### Expected Behavior

| Request | Cache Status | Behavior | First-token Delay |
|---------|--------------|----------|-------------------|
| 1st | Miss | Simple truncation + background task | ~1-2s |
| 2nd+ | Hit | Use cached summary | ~1-2s |
| After N msgs | Stale | Use cache + background update | ~1-2s |

---

## Smart Model Routing Configuration

Located in `api_server.py` around line 200-300.

### How It Works

The router intercepts Opus requests and decides whether to use Opus or Sonnet based on:

**Priority 0 (Whitelist - Highest Priority):**
   - Request header `X-Force-Model: opus` → Always Opus
   - Message contains `[FORCE_OPUS]` marker → Always Opus

**Priority 1 (Force Opus):**
   - Extended Thinking requests → Always Opus
   - Main Agent first turn → 35% Opus probability

**Priority 2 (Force Opus Keywords):**
   - Core tasks only: "创建项目", "系统设计", "架构设计", "整体重构", "整体规划"
   - Streamlined to 18 keywords (down from 30+)

**Priority 3 (Force Sonnet Keywords):**
   - Common operations: "看看", "显示", "修复", "运行", "调试", "优化", "配置"
   - Expanded to 60+ keywords for better coverage

**Priority 4 (Conversation Phase):**
   - First turn (≤2 messages): 50% Opus
   - Execution phase (≥5 tool calls): 85% Sonnet

**Priority 5 (Base Probability):**
   - Default: 15% Opus, 85% Sonnet

**Target Opus Usage: 20-25%** (optimized for high concurrency)

### Key Configuration Parameters

```python
MODEL_ROUTING_CONFIG = {
    "enabled": True,                              # Enable/disable routing
    "opus_model": "claude-opus-4-5-20251101",    # Target Opus model
    "sonnet_model": "claude-sonnet-4-5-20250929", # Target Sonnet model

    # Whitelist mechanism
    "whitelist_enabled": True,                    # Enable whitelist
    "whitelist_header": "X-Force-Model",          # Header name
    "whitelist_marker": "[FORCE_OPUS]",           # Message marker

    # Force Opus scenarios
    "force_opus_on_thinking": True,               # Extended Thinking → Opus
    "main_agent_opus_probability": 35,            # Main agent 35% Opus

    # Conversation detection
    "first_turn_opus_probability": 50,            # First turn 50% Opus
    "first_turn_max_user_messages": 2,            # ≤2 messages = first turn

    # Execution phase
    "execution_phase_tool_calls": 5,              # ≥5 tools = execution
    "execution_phase_sonnet_probability": 85,     # Execution 85% Sonnet

    # Base probability
    "base_opus_probability": 15,                  # Default 15% Opus
}
```

### Using the Whitelist

**Via Request Header:**
```bash
curl -X POST http://127.0.0.1:8100/v1/messages \
  -H "X-Force-Model: opus" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-4-5-20251101", "messages": [...]}'
```

**Via Message Marker:**
```json
{
  "model": "claude-opus-4-5-20251101",
  "messages": [
    {"role": "user", "content": "[FORCE_OPUS] This task requires Opus"}
  ]
}
```

### Tuning Guidelines

**To increase Opus usage:**
- Increase `first_turn_opus_probability` (e.g., 50 → 60)
- Increase `main_agent_opus_probability` (e.g., 35 → 45)
- Increase `base_opus_probability` (e.g., 15 → 20)
- Add more keywords to `force_opus_keywords`

**To decrease Opus usage (cost savings):**
- Decrease the above probabilities
- Add more keywords to `force_sonnet_keywords`
- Set `enabled: False` to disable routing entirely

### Monitor Routing Decisions

```bash
# View routing stats
curl http://127.0.0.1:8100/admin/routing/stats

# Reset stats
curl -X POST http://127.0.0.1:8100/admin/routing/reset

# Watch logs for routing decisions
tail -f /var/log/ai-history-manager.log | grep "模型路由"
```

---

## History Management Configuration

Located in `api_server.py` around line 44-62.

```python
HISTORY_CONFIG = HistoryConfig(
    strategies=[
        TruncateStrategy.PRE_ESTIMATE,    # Pre-estimate tokens
        TruncateStrategy.AUTO_TRUNCATE,   # Auto truncate
        TruncateStrategy.SMART_SUMMARY,   # AI summarization
        TruncateStrategy.ERROR_RETRY,     # Retry on length error
    ],
    max_messages=25,           # Max messages in history
    max_chars=100000,          # Max total characters
    summary_keep_recent=8,     # Keep N recent messages when summarizing
    summary_threshold=80000,   # Trigger summary at this char count
    retry_max_messages=15,     # Messages to keep on retry
    max_retries=3,             # Max retry attempts
    estimate_threshold=100000, # Pre-estimate threshold
)
```

### When to Adjust

| Symptom | Adjustment |
|---------|------------|
| "Input too long" errors | Decrease `max_chars`, `max_messages` |
| Context lost too quickly | Increase `summary_keep_recent` |
| Slow responses | Decrease `max_messages`, disable `SMART_SUMMARY` |
| Frequent retries | Increase `max_retries`, decrease thresholds |

---

## HTTP Connection Pool Configuration

Located in `api_server.py` around line 427-435.

```python
HTTP_POOL_MAX_CONNECTIONS = 1000    # Max concurrent connections
HTTP_POOL_MAX_KEEPALIVE = 200       # Keepalive connections
HTTP_POOL_KEEPALIVE_EXPIRY = 30     # Connection TTL (seconds)
HTTP_USE_HTTP2 = False              # Use HTTP/1.1 (not HTTP/2)
```

**Important:** HTTP/2 is disabled because the upstream API may treat multiplexed requests as coming from a single client, causing issues.

---

## Troubleshooting

### Service Won't Start

```bash
# 1. Check for port conflicts
lsof -i :8100

# 2. Force kill old processes
pkill -9 -f "uvicorn api_server:app"

# 3. Check Python dependencies
pip install -r requirements.txt

# 4. Try manual start for detailed errors
uvicorn api_server:app --host 0.0.0.0 --port 8100
```

### Tool Calls Not Working

Check logs for `parse_inline_tool_calls` errors:
```bash
grep -i "tool" /var/log/ai-history-manager.log | tail -20
```

Common issues:
- JSON parsing failure → Check `escape_json_string_newlines()`
- Incomplete JSON → Check `extract_json_from_position()`

### Response Truncation

If responses are being cut off:
1. Check `max_tokens` in request (default: 16384)
2. Look for `finish_reason: "length"` in logs
3. Increase `MAX_ALLOWED_TOKENS` if needed

### Routing Not Working as Expected

```bash
# Check current stats
curl http://127.0.0.1:8100/admin/routing/stats

# Example output:
# {"opus_requests": 10, "sonnet_requests": 40, "opus_sonnet_ratio": "1:4.0"}
```

If ratio is unexpected, check:
1. Keywords in `force_opus_keywords` / `force_sonnet_keywords`
2. Probability settings
3. User message content matching

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/v1/messages` | POST | Anthropic Messages API (main) |
| `/v1/chat/completions` | POST | OpenAI Chat API |
| `/v1/models` | GET | List models |
| `/admin/config` | GET | View configuration |
| `/admin/routing/stats` | GET | Routing statistics |
| `/admin/routing/reset` | POST | Reset routing stats |

---

## Log Analysis

Log format:
```
[request_id] 🔀 模型路由: original_model -> routed_model (reason)
```

Common routing reasons:
- `ExtendedThinking` - Extended thinking request
- `主Agent首轮(60%)` - Main agent first turn
- `关键词[xxx]` - Matched force_opus keyword
- `简单任务[xxx]` - Matched force_sonnet keyword
- `首轮对话(N条,90%)` - First conversation turn
- `执行阶段(N次工具,80%Sonnet)` - Execution phase
- `保底概率(30%)` - Base probability

---

## Quick Reference

```bash
# Start
bash start.sh

# Stop
pkill -9 -f "uvicorn api_server:app"

# Logs
tail -f /var/log/ai-history-manager.log

# Health
curl http://127.0.0.1:8100/

# Routing stats
curl http://127.0.0.1:8100/admin/routing/stats
```
