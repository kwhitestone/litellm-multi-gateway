# cc-litellm-gateway

以 [LiteLLM](https://github.com/BerriAI/litellm) 为底层的 Claude Code 网关，自带 **Admin UI + 多 provider 路由 + 用量记录**；并可选挂载一个 **vision 插件**，让「只支持文本」的 coding 端点（如火山方舟 ark coding plan）也能处理图片。

```
                    ┌─────────────────────────────────────────────┐
Claude Code ──────▶ │  vision(可选)  ──▶  litellm  ──▶  你的 provider │
http://127.0.0.1:   │  图片→文字        Admin UI     (ark/openai/…)   │
  4001 带视觉        │                  :4000                         │
  4000 仅核心        └─────────────────────────────────────────────┘
                       postgres(用量/虚拟 key)
```

## 为什么需要

- 想用**便宜的 coding plan**（纯文本端点），但它**不支持图片**。
- 又想要 **Admin UI 看用量、管 key、配多 provider**。
- vision 插件把图片先转成文字描述，再喂给纯文本端点 —— 报错截图 / UI 稿 / 流程图这类足够用。

## 两种用法

| 模式 | 启动命令 | Claude Code `ANTHROPIC_BASE_URL` |
|---|---|---|
| 仅核心网关 | `docker compose up -d` | `http://127.0.0.1:4000` |
| 核心 + 视觉插件 | `docker compose --profile vision up -d` | `http://127.0.0.1:4001` |

## 快速开始

前置：装好 Docker（Docker Desktop 或 Colima）。

```bash
git clone <repo> cc-litellm-gateway && cd cc-litellm-gateway
cp .env.example .env
# 编辑 .env：填 ARK_API_KEY（你的 coding provider token）和 Z_AI_API_KEY（视觉模型 key）

# 起核心 + 视觉插件
docker compose --profile vision up -d
# 等 ~40s（postgres 初始化 + litellm 迁移）
```

验证：
```bash
curl -s -o /dev/null -w "litellm: HTTP %{http_code}\n" http://127.0.0.1:4000/health/liveness   # 200
curl -s -o /dev/null -w "vision:  HTTP %{http_code}\n" http://127.0.0.1:4001/v1/messages -X POST # 400(缺body)=服务在
```

浏览器打开 **http://127.0.0.1:4000/ui** 看 Admin UI（登录见 `.env` 的 `UI_USERNAME/UI_PASSWORD`，或直接用 master key = `ARK_API_KEY`）。

## Claude Code 接入

编辑 `~/.claude/settings.json`（或你的配置 profile）：

```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",   // 带视觉用 4001；仅核心用 4000
    "ANTHROPIC_AUTH_TOKEN": "<填 .env 里的 ARK_API_KEY>",  // = litellm master_key
    "ANTHROPIC_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2"
  }
}
```

> `ANTHROPIC_AUTH_TOKEN` 必须等于 `ARK_API_KEY`（它就是 litellm 的 master_key），vision 插件转发时用它鉴权。

## 配置说明

### 换 provider（默认是 ark）

编辑 `litellm/config.yaml` 的 `model_list`，把 `model` / `api_base` / `api_key` 换成你的 provider。示例（OpenAI 兼容端点）：

```yaml
model_list:
  - model_name: glm-5.2
    litellm_params:
      model: openai/your-model          # 注意 provider 前缀决定格式
      api_base: https://your-provider/v1
      api_key: os.environ/ARK_API_KEY   # env 变量名可改
```

`model_name` 要和 Claude Code settings 里的 `*_MODEL` 一致。

### 换视觉模型（默认智谱 glm-4.6v）

`.env` 里改（任何 OpenAI 兼容的视觉模型都行）：
```
VISION_API_KEY=sk-...
VISION_BASE_URL=https://api.openai.com/v1
VISION_MODEL=gpt-4o
```

## vision 插件做了什么

转发到 litellm 之前，对 Anthropic `/v1/messages` 请求体做归一化：

- `{type:image}` → 调视觉模型转成文字描述（按 base64 的 sha256 缓存，不重复调用）
- `thinking` / `redacted_thinking` / `server_tool_use` 及其配对 `tool_result` → 剥离（纯文本端点不认）

SSE 流式响应、`tool_use` 原样透传，不影响 agentic 流程。

> 取舍：纯文本端点拿到的是「图片的文字描述」而非像素。看截图/稿/图够用；精确到像素的判断会失真。

## 常见问题

- **Admin UI 打不开 / 502**：本机有全局代理（Clash 等）把 localhost 也代理了。在代理规则放行 `127.0.0.1`/`localhost`，或临时关代理。
- **`No connected db`**：postgres 没起来或没 healthy。`docker compose ps` 看 db 状态。
- **端口 4000/4001 连不上（Colima）**：Colima 的端口转发偶尔抽风，`colima restart` 即可重建。
- **ark 报 `Model only support text input`**：说明 vision 没生效（请求没走 4001），或图片块没被转换。看 vision 容器日志 `docker compose logs vision`。

## 架构 / 文件

```
cc-litellm-gateway/
├─ docker-compose.yml     # litellm + db 核心；vision 为 profile 插件
├─ litellm/config.yaml    # provider 配置（模板，换这里）
├─ vision/                # 可选：图片→文字 + 归一化
│  ├─ Dockerfile
│  └─ app.py
├─ .env.example
└─ README.md
```

仅本地使用，所有端口只绑 `127.0.0.1`，不对外网暴露。
