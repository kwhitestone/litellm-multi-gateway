# vision 常驻 + 按 profile 自动决定转图/透传

## 背景
- 现状：vision 是 `profiles: ["vision"]` 可选插件，客户端按 profile 切 4000/4001，要换 base_url。
- 目标：vision 常驻，客户端**永远指 4001**，切 profile 不改 base_url；vision 按 profile 自动决定图片处理方式。

## 设计核心
profile 头部加结构化标记 `# native_vision: true|false`：
- `true`（claude/zai）：后端原生多模态，vision **原图透传**，不转文字。
- `false`（ark）：纯文本后端，vision **图片转文字**（现有行为）。

vision 容器挂载 config.yaml（只读），每次请求时 grep 该标记决定行为。读文件而非 env，因为 switch 只 restart litellm 不 restart vision，env 不会更新；文件能实时反映当前 profile。

## 改动清单

### 1. `litellm/profiles/*.yaml` — 加 native_vision 标记
- `ark.yaml`：`# native_vision: false`（纯文本，需转图）
- `zai.yaml`：`# native_vision: true`（原生多模态）
- `claude.yaml`：`# native_vision: true`（公司 Claude 网关原生支持图）

### 2. `vision/app.py` — 按 native_vision 决定图片处理
- 启动时读 `/app/config.yaml` 的 `# native_vision:` 标记（带缓存，每次请求重读以支持热切换）。
  - 文件不存在或标记缺失 -> 默认 `false`（保守，转图，向后兼容）。
- `_walk` 里 `type:image` / `image_url` 分支：
  - `native_vision=true` -> 原样保留 block（透传给后端）。
  - `native_vision=false` -> 现有转文字逻辑。
- 注意：`thinking`/`server_tool_use` 剥离逻辑**保持不变**（这些是协议归一化，与视觉无关，所有 profile 都需要）。

### 3. `docker-compose.yml` — vision 常驻
- 删 `profiles: ["vision"]`，vision 随 `docker compose up -d` 一起启动。
- vision 挂载 `./litellm/config.yaml:/app/config.yaml:ro`（读 native_vision 标记）。
- 顶部注释更新：启动命令统一为 `docker compose up -d`，客户端 BASE_URL 统一 4001。
- README 同步：去掉 `--profile vision` / 4000 vs 4001 的区分，统一 4001。

### 4. `profiles.sh` — switch 提示简化
- `cmd_switch` 末尾提示：不再区分 4000/4001，统一"客户端用 4001，docker compose up -d"。
- `cmd_new`：交互式加问"后端是否原生支持视觉(y/n)"，生成 `# native_vision:` 标记；默认 false。

### 5. README.md
- 顶部架构图、快速开始、Claude Code 接入、Hermes 接入：BASE_URL 统一 `http://127.0.0.1:4001`。
- "两种用法"表删掉 4000 行（或保留 4000 作为直连 litellm 调试用，标注"一般用 4001"）。
- vision 插件说明：补充"按 profile 自动决定转图/透传"。

## 风险与取舍
- **vision 成为单点**：所有流量过 vision。它只是轻量转发（无图时几乎零开销），可接受。
- **native_vision 标记靠注释**：litellm 忽略 yaml 注释，无副作用；缺点是不能被 litellm UI 识别，但这是 vision 层的 concern，不该污染 litellm 配置。
- **config.yaml 读取时机**：vision 每请求读文件有微小 IO 开销，可接受（本地文件）；或启动读+定时刷新。倾向每请求读（简单、热切换）。
- **向后兼容**：旧 profile 无标记 -> 默认 false（转图），等同 ark 行为，不破坏。

## 验证
1. `docker compose up -d` 后 4000+4001 都在。
2. 切到 claude：发带图请求，确认图片原图到达后端（不转文字）。
3. 切到 ark：发带图请求，确认图片被转成文字。
4. 切 profile 不重启 vision，行为随 config.yaml 切换。
