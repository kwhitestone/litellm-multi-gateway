<div align="center">

# 🚀 litellm-multi-gateway

**One gateway, multiple LLM backends. Route Claude Code, Hermes, or any OpenAI-compatible client to ARK / Anthropic / Zhipu — with Admin UI, per-client usage tracking, and automatic vision-to-text conversion for text-only backends.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/Docker-✓-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![LiteLLM](https://img.shields.io/badge/Powered%20by-LiteLLM-blueviolet)](https://github.com/BerriAI/litellm)
[![Claude Code](https://img.shields.io/badge/Works%20with-Claude%20Code-D97757)](https://docs.anthropic.com/en/docs/claude-code)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-ff69b4.svg)](CONTRIBUTING.md)

[English](#english) · [中文](#中文)

</div>

---

# English

A self-hosted **multi-backend AI gateway** built on [LiteLLM](https://github.com/BerriAI/litellm). It solves a real pain: you want to use a **cheap coding plan** (text-only endpoint like ARK/GLM), but it **doesn't support images**. Meanwhile, you also want an **Admin UI** for usage tracking, key management, and multi-provider routing — all from a single endpoint your clients never need to reconfigure.

```
Claude Code  ─(sk-cc-xxx, Anthropic)──▶┐  litellm (:4001)                              │
                                        │  ├─ vision_hook (auto image→text or passthrough)
Hermes / Any OpenAI client              │  ├─ router → provider (ARK/Anthropic/Zhipu)
       ─(sk-xxx, OpenAI)───────────────▶┤  ├─ Admin UI (:4001/ui)
             http://127.0.0.1:4001      │  └─ postgres (per-client usage tracking)
                                        └──────────────────────────────────────────────────┘
```

## ✨ Features

| Feature | Description |
|---|---|
| 🔀 **Multi-backend routing** | Load ARK, Anthropic, Zhipu (or any LiteLLM-supported provider) simultaneously. Route by virtual key aliases — switch backends without touching client config. |
| 🖼️ **Auto vision-to-text** | Text-only backends (ARK/GLM coding plans) automatically get images converted to text descriptions via a vision model. Multimodal backends get images passed through as-is. |
| 📊 **Admin UI + usage tracking** | Built-in LiteLLM Admin UI at `:4001/ui`. Per-client usage broken down by user/key. Create/manage virtual keys visually. |
| 🔑 **Virtual key management** | One `keys.sh` script handles everything: create, update routing, list, delete keys. Each client gets its own key — usage is tracked separately. |
| 🏠 **Dual protocol support** | Anthropic format (Claude Code) and OpenAI format (Hermes, OpenAI clients) connect to the same gateway simultaneously. |
| 🔒 **Local-only by default** | All ports bind to `127.0.0.1`. No external exposure unless you explicitly configure it. |

## Quick Start

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) (Docker Desktop or Colima)

```bash
git clone https://github.com/kwhitestone/litellm-multi-gateway.git
cd litellm-multi-gateway
cp .env.example .env
# Edit .env: add your provider API keys (ARK_API_KEY, CLAUDE_CODE_KEY, Z_AI_API_KEY)

docker compose up -d            # Starts litellm + postgres (~40s for first init)
```

Verify it's running:
```bash
curl -s -o /dev/null -w "Gateway: HTTP %{http_code}\n" http://127.0.0.1:4001/health/liveness   # → 200
```

Open **http://127.0.0.1:4001/ui** for the Admin UI.

### Connect Claude Code

```bash
./keys.sh new cc --backend claude   # Create a key routed to Anthropic backend
# Or: --backend ark / zai / ark,claude (comma-separated for multi-backend)
```

Add the returned key to `~/.claude/settings.json`:
```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",
    "ANTHROPIC_AUTH_TOKEN": "<your-virtual-key>"
  }
}
```

That's it. No `*_MODEL` env needed — Claude Code's default model names are routed by the key's aliases.

### Connect OpenAI-compatible clients (Hermes, etc.)

```bash
./keys.sh new hermes --backend ark,claude   # Multi-backend key
```

Point your client to:
- `base_url = http://127.0.0.1:4001/v1`
- `api_key = <your-virtual-key>`
- Send `model=ark` or `model=claude` to select backend

## How vision-to-text works

The gateway includes a [LiteLLM CustomLogger hook](litellm/hooks/vision_hook.py) that runs **after** routing is decided but **before** the request hits the backend:

- **`needs_vision: true`** (text-only backends like ARK/GLM): Images are sent to a vision model (default: Zhipu GLM-5V-Turbo, configurable to GPT-4o etc.) and converted to detailed text descriptions.
- **`needs_vision: false`** (multimodal backends like Anthropic): Images are passed through untouched.
- **All models**: Strips `thinking`/`server_tool_use` blocks and `cache_control.ttl` for protocol normalization.

The vision requirement is configured per-model in `multi.yaml` via simple comments:
```yaml
- model_name: ark-glm-5.2   # needs_vision: true
  litellm_params: { model: anthropic/glm-5.2, ... }
```

> **Trade-off:** With `needs_vision: true`, the backend receives a text description of the image rather than pixels. This works well for screenshots, UI mockups, and diagrams — but pixel-perfect tasks will have some loss.

## Key Management (`keys.sh`)

```bash
./keys.sh                          # Show help + list existing keys
./keys.sh new cc --backend ark     # Create key for user "cc" routed to ARK
./keys.sh new hermes --backend claude,zai  # Multi-backend key (send model=claude or model=zai)
./keys.sh list                     # List all keys with backend info
./keys.sh update <key> --backend zai  # Dynamically change routing (no restart needed!)
./keys.sh delete <key-prefix>      # Delete a key
```

## Configuration

### Backend config: `litellm/profiles/multi.yaml`

This is the LiteLLM runtime config (mounted directly by docker-compose). Edit provider keys, models, and `needs_vision` flags here. After changes: `docker compose restart litellm`.

### Vision model: `.env`

```bash
VISION_API_KEY=sk-...                          # Default: uses Z_AI_API_KEY
VISION_BASE_URL=https://api.openai.com/v1      # Default: Zhipu BigModel
VISION_MODEL=gpt-4o                            # Default: glm-5v-turbo
```

Any OpenAI-compatible vision model works.

## Architecture

```
litellm-multi-gateway/
├─ docker-compose.yml           # litellm + postgres (vision hook runs in-process)
├─ litellm/
│  ├─ profiles/multi.yaml       # LiteLLM config (ARK/Anthropic/Zhipu multi-backend)
│  └─ hooks/vision_hook.py      # CustomLogger: auto image→text or passthrough
├─ keys.sh                      # Virtual key management CLI
├─ .env.example                 # Configuration template
└─ README.md
```

## FAQ

<details>
<summary><b>Admin UI shows 502 / can't connect</b></summary>

You likely have a system proxy (Clash, etc.) intercepting localhost. Add `127.0.0.1`/`localhost` to your proxy bypass rules, or temporarily disable the proxy.
</details>

<details>
<summary><b><code>No connected db</code> error</b></summary>

Postgres isn't ready yet. Check with `docker compose ps` — wait for `db` to show `healthy`.
</details>

<details>
<summary><b>ARK reports <code>Model only support text input</code></b></summary>

Images aren't being converted. Check the model's `# needs_vision: true` flag in `multi.yaml`, and look for vision_hook logs: `docker compose logs litellm | grep vision_hook`.
</details>

<details>
<summary><b>Port 4001 unreachable (Colima)</b></summary>

Colima's port forwarding occasionally breaks. Run `colima restart` to rebuild it.
</details>

## Contributing

PRs welcome! This project aims to be a practical, no-nonsense gateway for multi-backend LLM routing. If you've added support for a new provider, improved the vision hook, or found a bug — open an issue or submit a PR.

## License

[MIT](LICENSE) © 2026 kwhitestone

---

# 中文

基于 [LiteLLM](https://github.com/BerriAI/litellm) 的**多后端 AI 网关**。核心解决一个痛点：你想用**便宜的 coding plan**（纯文本端点），但它**不支持图片**；同时又想要 **Admin UI 看用量、管 key、配多 provider**。

```
Claude Code  ─(sk-cc-xxx, anthropic)──▶┐  litellm(:4001)                           │
                                        │  ├─ vision_hook（按模型 needs_vision 转图/透传）
Hermes       ─(sk-xxx, openai)─────────▶┤  ├─ router → provider(ark/claude/zai，按 key alias)
       http://127.0.0.1:4001            │  ├─ Admin UI (:4001/ui)
                                        │  └─ postgres(用量按 user/key 分开统计)
```

## 为什么需要

- 想用**便宜的 coding plan**（纯文本端点），但它**不支持图片**。
- 又想要 **Admin UI 看用量、管 key、配多 provider**。
- 多个客户端（Claude Code、Hermes…）共用一个网关，但**用量要按客户端分开统计**。
- 客户端**固定指向一个地址**，切后端 profile 不用改 base_url；vision 按 profile 自动决定图片转文字还是原图透传。

## 快速开始

前置：装好 [Docker](https://docs.docker.com/get-docker/)（Docker Desktop 或 Colima）。

```bash
git clone https://github.com/kwhitestone/litellm-multi-gateway.git
cd litellm-multi-gateway
cp .env.example .env
# 编辑 .env：填三个后端的 key

docker compose up -d            # 起 litellm + postgres（首次约 40s）
```

验证：
```bash
curl -s -o /dev/null -w "litellm: HTTP %{http_code}\n" http://127.0.0.1:4001/health/liveness   # 200
```

浏览器打开 **http://127.0.0.1:4001/ui** 看 Admin UI。

### Claude Code 接入

```bash
./keys.sh new cc --backend claude   # 这个 key 走 claude 后端
# 或 --backend ark / zai / ark,claude（逗号多选）
```

填进 `~/.claude/settings.json`：
```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",
    "ANTHROPIC_AUTH_TOKEN": "<上一步返回的虚拟 key>"
  }
}
```

**不用配 `*_MODEL`**：Claude Code 发的默认名由虚拟 key 的 `aliases` 路由到指定后端。

### Hermes / OpenAI 客户端接入

```bash
./keys.sh new hermes --backend ark,claude   # 多后端 key
```

- `base_url = http://127.0.0.1:4001/v1`
- `api_key = <虚拟 key>`
- 发 `model=ark` 或 `model=claude` 选后端

## vision hook 做了什么

vision 是 litellm 的 CustomLogger（`litellm/hooks/vision_hook.py`），在请求路由到后端**之后**、发往后端**之前**运行：

- **`needs_vision: true`**（ark 等纯文本后端）：图片块用视觉模型转成文字描述
- **`needs_vision: false`**（claude/zai 原生多模态）：图片块原图透传
- **所有模型**：剥离 `thinking`/`server_tool_use` 及配对 `tool_result` + 剥掉 `cache_control.ttl`

在 `multi.yaml` 里用注释标记：
```yaml
- model_name: ark-glm-5.2   # needs_vision: true
  litellm_params: { ... }
```

> **取舍**：`needs_vision: true` 时后端拿到的是图片的文字描述而非像素，看截图/UI/图够用；`false` 时原图透传无损失。

## Key 管理（`keys.sh`）

```bash
./keys.sh                          # 帮助 + 现有 key 列表
./keys.sh new cc --backend ark     # 创建绑 user=cc 的 key，走 ark 后端
./keys.sh new hermes --backend claude,zai  # 多后端 key
./keys.sh list                     # 列出所有 key
./keys.sh update <key> --backend zai  # 动态改路由（不重启 litellm，秒级生效）
./keys.sh delete <key-prefix>      # 删除 key
```

## 配置

- **后端**：`litellm/profiles/multi.yaml`（直接编辑，`docker compose restart litellm` 生效）
- **视觉模型**：`.env` 里 `VISION_API_KEY` / `VISION_BASE_URL` / `VISION_MODEL`（任何 OpenAI 兼容视觉模型都行）

## 常见问题

- **Admin UI 502**：全局代理拦截了 localhost，放行 `127.0.0.1` 或关代理。
- **`No connected db`**：postgres 没起来，`docker compose ps` 看状态。
- **ark 报 `Model only support text input`**：图片没被转换，检查 `needs_vision: true` 标记。
- **端口不通（Colima）**：`colima restart`。

## 开源协议

[MIT](LICENSE) © 2026 kwhitestone
