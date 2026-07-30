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
# 编辑 .env：填 ARK_API_KEY（你的 coding provider token）和 Z_AI_API_KEY（视觉模型 key）

./profiles.sh switch ark    # 生成 litellm/config.yaml（默认 ark；可换 ./profiles.sh switch zai）

# 启动（litellm + postgres + vision 一起起）
docker compose up -d
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
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",   // 固定 4001，切 profile 不用换
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

### 切换 / 管理 profile（一键切后端）

`litellm/profiles/` 里预置了几套 provider 配置，用 `profiles.sh` 管理（自动重启 litellm）：

```bash
./profiles.sh                       # 看有哪些 profile + 当前在用哪个（无参数 = list）
./profiles.sh switch ark            # 切到火山方舟 coding plan（纯文本，配 vision）
./profiles.sh switch zai            # 切到智谱 BigModel（原生多模态，无需 vision）
./profiles.sh new <name> [opts]     # 生成新 profile（交互式或带参数免交互）
./profiles.sh delete <name>         # 删除 profile（当前在用的不让删）
```

切换后客户端 BASE_URL 不变（始终 4001）；vision 会按 profile 的 `native_vision` 标记自动决定图片转文字（ark）还是原图透传（zai/claude）。

> 想加自己的 profile？两种方式：
>
> - **脚本化（推荐）**：`./profiles.sh new myprofile`（交互式问答），或带参数免交互 `./profiles.sh new myprofile --model glm-4.7 --base https://... --key-env Z_AI_API_KEY --proto anthropic`。自动生成全套 Claude Code 别名，结构与 ark/zai 一致。
> - **手动**：复制 `litellm/profiles/ark.yaml` 改一改，文件名就是 profile 名（`./profiles.sh switch 你的名`）。

**关于模型名映射**：Claude Code 不配 `*_MODEL` 时会发默认的 Anthropic 模型名（`claude-sonnet-5` 等）。每个 profile 的 `model_list` 里把这些名字都列出来、指向你的实际模型，LiteLLM 就会自动转换——所以 Claude Code 配置可以极简（只留 BASE_URL + token），模型路由全由 LiteLLM 接管。

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
curl -X POST http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"user_id":"cc","key_alias":"cc","models":["glm-5.2","claude-sonnet-5"]}'

# 给 Hermes 建 key（绑 user=hermes）
curl -X POST http://127.0.0.1:4000/key/generate \
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
