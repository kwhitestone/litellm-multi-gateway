# 📣 litellm-multi-gateway 推广文案 & 渠道清单

> 生成时间: 2026-07-30  
> Repo: https://github.com/kwhitestone/litellm-multi-gateway

---

## 📝 各平台文案

### 1. V2EX（中文技术社区）

**节点选择**: `#分享创造` 或 `#程序员`

**标题**: 开源了一个多后端 AI 网关，Claude Code + Hermes 共用一个端口，便宜 coding plan 也能看图

**正文**:

> 搞了个基于 LiteLLM 的多后端 AI 网关，解决一个很实际的痛点：
>
> 想用便宜的 coding plan（比如火山 ARK 的 GLM），但它不支持图片输入。同时想要 Admin UI 看用量、管 key、多 provider 路由。
>
> **核心功能**：
> - 🔀 多后端共存（ARK/Anthropic/智谱），用虚拟 key 按后端路由，切后端不用改客户端配置
> - 🖼️ 纯文本后端自动把图片转成文字描述（vision hook），多模态后端原图透传
> - 📊 Admin UI + 用量按客户端分开统计
> - 🔑 一个脚本管所有 key（创建/路由/删除），动态改路由不用重启
> - 🏠 Anthropic 格式 + OpenAI 格式同时接入
>
> 一行 docker compose up 搞定，所有端口只绑 127.0.0.1。
>
> GitHub: https://github.com/kwhitestone/litellm-multi-gateway
>
> 觉得有用的话给个 ⭐ 支持一下！有问题欢迎提 issue。

---

### 2. Reddit (r/ClaudeAI)

**Title**: [Open Source] Built a multi-backend AI gateway for Claude Code — route to cheap LLM providers, auto image-to-text for text-only backends, Admin UI with per-client usage tracking

**Body**:

> Hey everyone! I open-sourced a self-hosted gateway built on LiteLLM that lets you:
>
> - **Route Claude Code to multiple LLM backends** (ARK/GLM, Anthropic, Zhipu) from a single endpoint
> - **Use cheap text-only coding plans** even when they don't support images — the gateway auto-converts images to text descriptions via a vision model
> - **Track usage per client** with virtual keys and a built-in Admin UI
> - **Connect both Anthropic-format (Claude Code) and OpenAI-format clients** simultaneously
>
> The key insight: you shouldn't have to choose between a cheap provider and image support. This gateway handles the mismatch transparently with a LiteLLM CustomLogger hook that runs in-process.
>
> **One-line setup**: `docker compose up -d`
>
> 🔗 GitHub: https://github.com/kwhitestone/litellm-multi-gateway
>
> It's MIT licensed and self-hosted (all ports bind to localhost by default). Feedback and PRs welcome!

---

### 3. Reddit (r/LocalLLaMA)

**Title**: Self-hosted multi-backend LLM gateway — route Claude Code / OpenAI clients to GLM/Claude/Zhipu with auto image-to-text conversion

**Body**:

> Built a gateway on LiteLLM that solves a real problem I had: using GLM coding plans (text-only) with Claude Code (which sends images).
>
> **How it works**: A CustomLogger hook inspects requests after routing but before they hit the backend. If the backend is text-only (marked `needs_vision: true` in config), images are sent to a vision model and converted to text descriptions. If the backend supports multimodal, images pass through untouched.
>
> Features:
> - Multi-backend routing via virtual keys (ARK/Anthropic/Zhipu simultaneously)
> - Admin UI with per-client usage tracking
> - Dynamic backend switching without restart
> - Dual protocol: Anthropic + OpenAI format clients
>
> 🔗 https://github.com/kwhitestone/litellm-multi-gateway
>
> MIT licensed, Docker-based, self-hosted. Stars and feedback appreciated! 🙏

---

### 4. Hacker News

**Title**: Show HN: Multi-backend AI gateway with auto image-to-text for text-only LLM providers

**Body**:

> Hi HN, I built a self-hosted gateway on top of LiteLLM that addresses a gap in the current LLM tooling ecosystem.
>
> The problem: cheap coding-focused LLM plans (like Volcano Engine's ARK GLM plans) are text-only — they reject image inputs. But developer tools like Claude Code routinely send screenshots and UI images. You're forced to either pay for an expensive multimodal provider or lose image support entirely.
>
> This gateway bridges that gap with a CustomLogger hook that runs after routing is decided but before the request reaches the backend. It inspects the target model's capabilities (configured in YAML) and either converts images to text descriptions via a vision model, or passes them through as-is.
>
> Additional features:
> - Multiple backends loaded simultaneously, routed by virtual key aliases
> - Built-in Admin UI (LiteLLM's) with per-client usage tracking
> - Dynamic backend switching without restarting the gateway
> - Supports both Anthropic and OpenAI API formats simultaneously
>
> It's MIT-licensed, runs via Docker Compose, and binds to localhost by default.
>
> Source: https://github.com/kwhitestone/litellm-multi-gateway
>
> I'd love feedback on the vision-to-text approach — it's a pragmatic trade-off (you lose pixel-level accuracy) but works well for the common case of screenshots and UI debugging.

---

### 5. X/Twitter (Thread)

**Tweet 1**:
🚀 Just open-sourced litellm-multi-gateway — a self-hosted multi-backend AI gateway built on @LiteLLM.

Route Claude Code / Hermes / any OpenAI client to multiple LLM providers (ARK/Anthropic/Zhipu) from one endpoint.

Features 👇 [thread]

**Tweet 2**:
🖼️ The killer feature: text-only LLM backends (like cheap GLM coding plans) automatically get images converted to text descriptions via a vision model.

Multimodal backends get images passed through as-is.

No code changes needed in your clients.

**Tweet 3**:
📊 Built-in Admin UI with per-client usage tracking.
🔑 One-command key management with dynamic routing (switch backends without restart!).
🏠 Anthropic + OpenAI format clients connect simultaneously.
🔒 Localhost-only by default.

**Tweet 4**:
One line to start:
```
docker compose up -d
```

MIT licensed, self-hosted, Docker-based.

🔗 https://github.com/kwhitestone/litellm-multi-gateway

Stars appreciated! ⭐🙏

---

### 6. 小红书 / 即刻 / 少数派（中文短文）

**标题**: 开源｜让便宜的大模型 coding plan 也能看图，一个网关搞定多后端路由

**正文**:

> 搞了个开源项目：litellm-multi-gateway 🚀
>
> 痛点：便宜的 coding plan（GLM 等）不支持图片，但 Claude Code 经常需要发截图。
>
> 解决方案：基于 LiteLLM 的网关，自动检测后端是否支持图片——
> - 纯文本后端：图片自动转文字描述（用视觉模型）
> - 多模态后端：原图直传
>
> 还附带 Admin UI 看用量、虚拟 key 管理、多后端同时在线。
>
> docker compose up -d 一键启动，MIT 协议。
>
> GitHub 搜 litellm-multi-gateway，觉得有用给个 star ⭐

---

### 7. GitHub Discussions / LiteLLM 社区

**在 LiteLLM 仓库提 Discussion**:

> Title: Show & Tell — Multi-backend gateway with auto image-to-text hook
>
> Hi! I built a project on top of LiteLLM that I thought might be useful to others:
>
> https://github.com/kwhitestone/litellm-multi-gateway
>
> It's a Docker Compose setup that:
> - Loads multiple providers simultaneously (ARK/Anthropic/Zhipu)
> - Uses a CustomLogger hook to auto-convert images to text for text-only backends
> - Manages virtual keys via a CLI script with dynamic routing
>
> The vision hook (`VisionPreRequestHook`) might be interesting to others dealing with mixed multimodal/text-only backends. It parses `needs_vision` flags from the config YAML comments to decide per-model behavior.
>
> Would love any feedback!

---

## 📋 渠道优先级 & 发布建议

| 优先级 | 渠道 | 为什么 | 何时发 |
|---|---|---|---|
| 🔴 P0 | V2EX #分享创造 | 中文开发者密度最高的社区，对 dev tools 非常友好 | 工作日 10:00-12:00 或 20:00-22:00 |
| 🔴 P0 | Reddit r/ClaudeAI | Claude Code 用户精准受众，对网关/代理话题高度敏感 | 美东时间 8-10AM (北京时间 21-23点) |
| 🟠 P1 | Reddit r/LocalLLaMA | 自部署 LLM 社区，对多后端路由有需求 | 同上 |
| 🟠 P1 | Hacker News (Show HN) | 流量巨大，但竞争激烈，需要标题足够吸引 | 美东时间 8-10AM 周二-周四最佳 |
| 🟡 P2 | X/Twitter | 技术社区影响力大，但需要粉丝基础才有传播 | 任何时间，配合 thread 效果好 |
| 🟡 P2 | 即刻 | 中文 tech 社区，对开源项目友好 | 晚上 20:00-22:00 |
| 🟢 P3 | 小红书/少数派 | 泛科技受众，流量泛但不精准 | 周末 |
| 🟢 P3 | LiteLLM Discussions | 被官方项目引用是最高质量的 backlink | 随时 |

## 🎯 额外涨星策略

### 短期（1-2周）
1. **提交到 awesome lists**（PR 到以下仓库）:
   - `awesome-litellm`（如果存在）
   - `awesome-ai-tools`
   - `awesome-selfhosted`
   - `awesome-claude-code`

2. **在 LiteLLM 的 GitHub Discussions 发帖** — 最精准的用户池

3. **GitHub Topics 优化** — 已完成 ✅ (添加了 12 个 topics)

### 中期（1-3月）
4. **写技术博客** — 详细的 "How I built a multi-backend AI gateway" 文章，发到:
   - dev.to
   - Medium
   - 掘金
   - 个人博客（如果有）

5. **录制演示视频/GIF** — Admin UI 操作 + vision 转图效果，放到 README 顶部

6. **Star 交换** — 在 GitHub 搜索类似项目，star 他们的，他们可能会回 star

### 长期
7. **持续维护** — 快速响应 issue，合并 PR，这比任何营销都有效
8. **版本发布** — 用 GitHub Releases 打 tag，会出现在 GitHub explore feed
9. **集成更多 provider** — OpenRouter/Together/Groq 等热门 provider 支持会扩大受众
