# litellm-multi-gateway

以 [LiteLLM](https://github.com/BerriAI/litellm) 为底层的多客户端 AI 网关，自带 **Admin UI + 多 provider 路由 + 用量按客户端分开统计**；vision 作为 litellm 的 **CustomLogger hook** 运行在进程内，按模型 needs_vision 标记决定：纯文本后端（如 ark coding plan）把图片转文字，原生多模态后端（如 zai/claude）原图透传。支持 ark/claude/zai 多后端同时加载，用虚拟 key 的 aliases 按后端路由。支持 Anthropic 格式（Claude Code 等）和 OpenAI 格式（Hermes 等）客户端同时接入。

```
Claude Code  ─(sk-cc-xxx, anthropic)──▶┐  litellm(:4001)                           │
                                       │  ├─ vision_hook（按模型 needs_vision 转图/透传）
Hermes       ─(sk-hermes-yyy, openai)─▶┤  ├─ router → provider(ark/claude/zai，按 key alias)
        http://127.0.0.1:4001          │  └─ Admin UI                              │
                                       └──── postgres(用量按 user/key 分开统计) ────┘
```

## 为什么需要

- 想用**便宜的 coding plan**（纯文本端点），但它**不支持图片**。
- 又想要 **Admin UI 看用量、管 key、配多 provider**。
- 多个客户端（Claude Code、Hermes…）共用一个网关，但**用量要按客户端分开统计**。
- 客户端**固定指向一个地址**，切后端 profile 不用改 base_url；vision 按 profile 自动决定图片转文字还是原图透传。

## 用法

| 启动命令 | 客户端 BASE_URL |
|---|---|
| `docker compose up -d` | `http://127.0.0.1:4001`（Anthropic）/ `http://127.0.0.1:4001/v1`（OpenAI） |

> litellm 直接监听 4001（含 Admin UI: http://127.0.0.1:4001/ui）。vision 已是 litellm 内的 hook，无独立容器。

## 快速开始

前置：装好 Docker（Docker Desktop 或 Colima）。

```bash
git clone <repo> litellm-multi-gateway && cd litellm-multi-gateway
cp .env.example .env
# 编辑 .env：填三个后端的 key —— ARK_API_KEY（兼作 master key）、CLAUDE_CODE_KEY、Z_AI_API_KEY

./profiles.sh switch            # 生成 litellm/config.yaml（multi：多后端共存）

docker compose up -d            # 起 litellm + postgres（vision 是 litellm 内的 hook，无独立容器）
# 等 ~40s（postgres 初始化 + litellm 迁移）
```

验证：
```bash
curl -s -o /dev/null -w "litellm: HTTP %{http_code}\n" http://127.0.0.1:4001/health/liveness   # 200
```

浏览器打开 **http://127.0.0.1:4001/ui** 看 Admin UI（登录用 `.env` 的 `UI_USERNAME/UI_PASSWORD`，或 master key = `ARK_API_KEY`）。

## Claude Code 接入

先建一个虚拟 key（**不要用 master key**）：

```bash
./keys.sh new cc --backend claude   # 这个 key 走 claude 后端（公司网关）
# 或 --backend ark / zai / ark,claude（逗号多选）
```

把返回的 key 填进 `~/.claude/settings.json`：

```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",
    "ANTHROPIC_AUTH_TOKEN": "<上一步返回的虚拟 key>"
  }
}
```

**不用配 `*_MODEL`**：Claude Code 发的默认名 `claude-sonnet-5` 等，由虚拟 key 的 `aliases` 路由到 `--backend` 指定的后端。换后端只需换 key，或建多后端 key（`--backend ark,claude`，发 `model=ark`/`model=claude` 选）。

> 客户端一律用 `keys.sh new` 创建的虚拟 key。不要填 `.env` 里的 `ARK_API_KEY`——那是 master key，仅用于网关管理，用了用量也无法按客户端分开。

## 配置说明

### 后端（multi profile）

`litellm/profiles/multi.yaml` 把 ark/claude/zai 三个后端**同时加载**在一个 config。加新后端、改 api_base/key 都编辑这个文件，改完 `./profiles.sh switch` 重新生成 config 并重启 litellm。每个模型带 `# needs_vision:` 标记，vision hook 据此决定转图（ark）或原图透传（claude/zai）。

### 换视觉模型（默认智谱 glm-4.6v）

`.env` 里改（任何 OpenAI 兼容的视觉模型都行）：
```
VISION_API_KEY=sk-...
VISION_BASE_URL=https://api.openai.com/v1
VISION_MODEL=gpt-4o
```

## 多后端共存（multi profile）

`multi` profile 把 ark/claude/zai 三个后端**同时加载**在一个 config.yaml，不再 switch 切换。用虚拟 key 的 `aliases` 按后端路由：

```bash
./profiles.sh switch multi        # 切到多后端模式
docker compose up -d

# 给 key 配后端（cc 默认名会 alias 到指定后端，Claude Code 不改配置即可走）
./keys.sh new cc --backend ark          # 这个 key 走 ark
./keys.sh new col --backend claude      # 这个 key 走 claude
./keys.sh new me --backend ark,claude   # 多后端 key：发 model=ark 或 model=claude 选后端
```

后端模型命名：`ark-glm-5.2` / `claude-sonnet-5`（claude 用真名，cc 默认名直接命中）/ `zai-glm-4.7`。单后端 profile（ark/zai/claude）仍可用 `./profiles.sh switch <name>`。

## vision hook 做了什么

vision 是 litellm 的 CustomLogger（`litellm/hooks/vision_hook.py`），在请求路由到后端**之后**、发往后端**之前**运行——此时已知真实后端模型，能精确按模型配置决定：

- **`needs_vision: true`（ark 等纯文本后端）**：图片块用视觉模型转成文字描述
- **`needs_vision: false`（claude/zai 原生多模态）**：图片块原图透传
- 所有模型：剥离 `thinking`/`server_tool_use` 及配对 `tool_result`（协议归一化）+ 剥掉 `cache_control.ttl`（避免 1h/5m 顺序冲突）

模型是否需要 vision 在 config.yaml 的 model_list 里标记（`# needs_vision: true/false`），头部 `# native_vision:` 作无标记时的回退默认。转文字模式下图片按 sha256/URL 缓存。

> 取舍：`needs_vision: true` 时后端拿到的是「图片的文字描述」而非像素，看截图/稿/图够用；`false` 时原图透传无此损失。

## 多客户端 + 用量分开统计（虚拟 key）

给每个客户端发一个独立 key（绑到不同 user），用量自动按 user/key 分开。`cc`（Anthropic 格式）和 `hermes`（OpenAI 格式）可同时接入：

```bash
MASTER=$(grep '^ARK_API_KEY=' .env | cut -d= -f2)

# 给 Claude Code 建 key（绑 user=cc）
curl -X POST http://127.0.0.1:4001/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"user_id":"cc","key_alias":"cc","models":["glm-5.2","claude-sonnet-5"]}'

# 给 Hermes 建 key（绑 user=hermes）
curl -X POST http://127.0.0.1:4001/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"user_id":"hermes","key_alias":"hermes","models":["glm-5.2","claude-sonnet-5"]}'
```

> key 只在创建时返回一次明文，务必保存。也可在 Admin UI → API Key Users / Keys 页面图形化创建。

客户端配置（**用各自的 key，统一指向 4001**）：

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
- **ark 报 `Model only support text input`**：说明图片没被转换。检查模型的 `# needs_vision:` 是否为 true，看 litellm 日志 `docker compose logs litellm | grep vision_hook`（应显示"转文字"）。

## 架构 / 文件

```
litellm-multi-gateway/
├─ docker-compose.yml     # litellm + db + vision（vision 常驻）
├─ litellm/config.yaml    # provider 配置（模板，换这里）
├─ litellm/profiles/      # 预制 provider 配置（ark/zai/…），profiles.sh 切换
├─ profiles.sh            # 管理 profile（new/switch/list/delete）
├─ keys.sh                # 管理客户端虚拟 key（创建/列表/删除）
├─ litellm/hooks/        # vision_hook 等 CustomLogger（litellm callbacks 加载）
├─ .env.example
└─ README.md
```

仅本地使用，所有端口只绑 `127.0.0.1`，不对外网暴露。
