# litellm-multi-gateway

以 [LiteLLM](https://github.com/BerriAI/litellm) 为底层的多客户端 AI 网关，自带 **Admin UI + 多 provider 路由 + 用量按客户端分开统计**；并可选挂载一个 **vision 插件**，让「只支持文本」的 coding 端点（如火山方舟 ark coding plan）也能处理图片。支持 Anthropic 格式（Claude Code 等）和 OpenAI 格式（Hermes 等）客户端同时接入。

```
Claude Code  ─(sk-cc-xxx, anthropic)──▶
                                     ┌──────────────────────────────────────────┐
Hermes       ─(sk-hermes-yyy, openai)─▶  vision(可选) ──▶ litellm ──▶ provider   │
        http://127.0.0.1:4001          │  图片→文字       Admin UI    (ark/zai/…) │
        http://127.0.0.1:4000 (仅核心)  │                 :4000                   │
                                     └──────────────────────────────────────────┘
                                         postgres(用量按 user/key 分开统计)
```

## 为什么需要

- 想用**便宜的 coding plan**（纯文本端点），但它**不支持图片**。
- 又想要 **Admin UI 看用量、管 key、配多 provider**。
- 多个客户端（Claude Code、Hermes…）共用一个网关，但**用量要按客户端分开统计**。
- vision 插件把图片先转成文字描述，再喂给纯文本端点 —— 报错截图 / UI 稿 / 流程图这类足够用。

## 两种用法

| 模式 | 启动命令 | 客户端 BASE_URL |
|---|---|---|
| 仅核心网关 | `docker compose up -d` | `http://127.0.0.1:4000` |
| 核心 + 视觉插件 | `docker compose --profile vision up -d` | `http://127.0.0.1:4001` |

## 快速开始

前置：装好 Docker（Docker Desktop 或 Colima）。

```bash
git clone <repo> litellm-multi-gateway && cd litellm-multi-gateway
cp .env.example .env
# 编辑 .env：填 ARK_API_KEY（你的 coding provider token）和 Z_AI_API_KEY（视觉模型 key）

./switch.sh ark          # 生成 litellm/config.yaml（默认 ark；可换 ./switch.sh zai）

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

### 切换预制 profile（一键切后端）

`litellm/profiles/` 里预置了几套 provider 配置，用 `switch.sh` 一键切换（自动重启 litellm）：

```bash
./switch.sh            # 看有哪些 profile + 当前在用哪个
./switch.sh ark        # 火山方舟 coding plan（纯文本，配 vision）
./switch.sh zai        # 智谱 BigModel（原生多模态，无需 vision）
```

切换后按提示调整：ark 用 `--profile vision` + BASE_URL `:4001`；zai 不带 vision + BASE_URL `:4000`。

> 想加自己的 profile？复制 `litellm/profiles/ark.yaml` 改一改，文件名就是 profile 名（`./switch.sh 你的名`）。

**关于模型名映射**：Claude Code 不配 `*_MODEL` 时会发默认的 Anthropic 模型名（`claude-sonnet-5` 等）。每个 profile 的 `model_list` 里把这些名字都列出来、指向你的实际模型，LiteLLM 就会自动转换——所以 Claude Code 配置可以极简（只留 BASE_URL + token），模型路由全由 LiteLLM 接管。

### 换视觉模型（默认智谱 glm-4.6v）

`.env` 里改（任何 OpenAI 兼容的视觉模型都行）：
```
VISION_API_KEY=sk-...
VISION_BASE_URL=https://api.openai.com/v1
VISION_MODEL=gpt-4o
```

## vision 插件做了什么

转发到 litellm 之前，对请求体做归一化（同时支持两种格式）：

- **Anthropic `/v1/messages`**：`{type:image, source}` → 转文字；`thinking`/`server_tool_use` 及配对 `tool_result` → 剥离
- **OpenAI `/v1/chat/completions`**：`{type:image_url, image_url:{url}}` → 转文字（支持 data:base64 和 http(s) URL）

图片按内容 sha256 / URL 缓存，不重复调用视觉模型。SSE 流式响应、`tool_use` 原样透传。

> 取舍：纯文本端点拿到的是「图片的文字描述」而非像素。看截图/稿/图够用；精确到像素的判断会失真。

## 多客户端 + 用量分开统计（虚拟 key）

给每个客户端发一个独立 key（绑到不同 user），用量自动按 user/key 分开。`cc`（Anthropic 格式）和 `hermes`（OpenAI 格式）可同时接入：

```bash
MASTER=$(grep '^ARK_API_KEY=' .env | cut -d= -f2)

# 给 Claude Code 建 key（绑 user=cc）
curl -X POST http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"user_id":"cc","key_alias":"cc","models":["glm-5.2","claude-sonnet-5"]}'

# 给 Hermes 建 key（绑 user=hermes）
curl -X POST http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"user_id":"hermes","key_alias":"hermes","models":["glm-5.2","claude-sonnet-5"]}'
```

> key 只在创建时返回一次明文，务必保存。也可在 Admin UI → API Key Users / Keys 页面图形化创建。

客户端配置（**用各自的 key，指向 4001 走视觉，或 4000 走纯核心**）：

```jsonc
// Claude Code (~/.claude/settings.json)
{ "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",
    "ANTHROPIC_AUTH_TOKEN": "<cc 的 key>"
}}

// Hermes (OpenAI 格式)
// base_url = http://127.0.0.1:4001/v1   api_key = <hermes 的 key>
```

在 Admin UI → **Usage** 页按 `User` / `API Key` 筛选，即可分别看到 cc、hermes 各自的用量；vision 转发时保留客户端原始 key，所以统计归属准确。

## 常见问题

- **Admin UI 打不开 / 502**：本机有全局代理（Clash 等）把 localhost 也代理了。在代理规则放行 `127.0.0.1`/`localhost`，或临时关代理。
- **`No connected db`**：postgres 没起来或没 healthy。`docker compose ps` 看 db 状态。
- **端口 4000/4001 连不上（Colima）**：Colima 的端口转发偶尔抽风，`colima restart` 即可重建。
- **ark 报 `Model only support text input`**：说明 vision 没生效（请求没走 4001），或图片块没被转换。看 vision 容器日志 `docker compose logs vision`。

## 架构 / 文件

```
litellm-multi-gateway/
├─ docker-compose.yml     # litellm + db 核心；vision 为 profile 插件
├─ litellm/config.yaml    # provider 配置（模板，换这里）
├─ vision/                # 可选：图片→文字 + 归一化
│  ├─ Dockerfile
│  └─ app.py
├─ .env.example
└─ README.md
```

仅本地使用，所有端口只绑 `127.0.0.1`，不对外网暴露。
