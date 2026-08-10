# 线上部署指南

单容器镜像部署到公共云（Railway / Render / Fly / 任意能跑 Docker 的主机）。
Postgres 用平台托管的实例，通过 `DATABASE_URL` 注入；key 管理全程走可视化页面，不依赖 CLI。

---

## 它是怎么跑起来的

镜像基于 `ghcr.io/berriai/litellm:main-stable`，叠加了我们自己的东西：

```
/app/
├─ config.yaml        ← 构建期由 gen_config.py 从 backends.yaml 生成（后端结构固化，不含密钥）
├─ server.py          ← 容器入口（覆盖了父镜像的 ENTRYPOINT）
├─ gen_config.py      ← 后端别名解析器（管理页和它共用逻辑）
├─ hooks/vision_hook.py   ← 图片转文字 / 协议归一化的 CustomLogger
├─ schema.prisma      ← LiteLLM 自带，db 迁移用
└─ manage/            ← 自建管理页（FastAPI 路由 + 模板）
```

`server.py` 做三件事，**单进程、单端口**：
1. 先跑 `prisma db push` 给空库建表（首次部署必需，否则创建 key 会 500）
2. `import` LiteLLM 原生的 FastAPI `app`，把管理页 router 挂上去（`app.include_router`）
3. `uvicorn.run(app)` 启动

所以 LiteLLM 的所有端点（`/v1/messages`、`/v1/chat/completions`、`/ui`、`/key/*`）和我们的管理页（`/manage/*`）共用同一个进程和端口。没有 supervisor、没有第二个进程、没有反向代理。

---

## 1. 构建镜像

在仓库根目录：

```bash
docker build -t litellm-gateway .
```

构建过程（约 2-3 分钟，取决于网络）：
- 拉取 `litellm:main-stable` 基础镜像
- `COPY` 进 backends.yaml / gen_config.py / vision_hook.py / manage/ / server.py
- `ensurepip` 引导 + `pip install psycopg2-binary`（管理页审计要写 PG，镜像本身没这个库）
- 构建期跑 `gen_config.py gen-config` 生成 `/app/config.yaml`（密钥不进镜像，运行时从环境变量读）

镜像里**不含任何密钥**——所有 `*_API_KEY` 都是运行时从环境变量注入。

---

## 2. 必需环境变量

### 必填

| 变量 | 说明 | 示例 |
|---|---|---|
| `DATABASE_URL` | 托管 Postgres 连接串。LiteLLM 存 key/用量/审计，管理页存登录审计 | `postgresql://user:pass@host:5432/db` |
| `GATEWAY_MASTER_KEY` | 网关管理密钥。**单独生成随机串**，别复用 provider key。登录管理页用它，吊销会话改它 | 用 `openssl rand -hex 32` 生成 |
| `ARK_API_KEY` | 火山 ark coding plan token（对应 ark 后端） | `ark-xxxxxxxx-xxxx-...` |
| `CLAUDE_CODE_KEY` | 公司 Claude 网关 key（对应 claude 后端） | `sk-xxxxxxxx` |
| `Z_AI_API_KEY` | 智谱 BigModel key（对应 zai 后端；vision hook 默认视觉模型也用它） | `your-zhipu-key` |
| `UI_USERNAME` | LiteLLM 原生 Admin UI 登录用户名 | `admin` |
| `UI_PASSWORD` | LiteLLM 原生 Admin UI 登录密码。**务必强密码** | （随机强密码） |

> `GATEWAY_MASTER_KEY` 和 `UI_PASSWORD` 是两套不同的凭据：
> - `GATEWAY_MASTER_KEY` 管我们的 `/manage/` 页面 + 调 LiteLLM 管理 API（创建/删除 key）
> - `UI_USERNAME`/`UI_PASSWORD` 管 LiteLLM 原生 `/ui` 页面

### 选填

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `4001` | 容器监听端口。PaaS 通常自动注入 `PORT`，此时会覆盖默认值 |
| `CLAUDE_CODE_KEY_1` | （空） | claude_1 后端的独立 key（隔离用量）。不填则 claude_1 后端不可用 |
| `CLAUDE_CODE_KEY_2` | （空） | claude_2 后端的独立 key。同上 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:{PORT}` | 对外公开地址。管理页创建 key 后展示给客户端的 BASE_URL。线上填你的 HTTPS 域名 |
| `MANAGE_SESSION_TTL_HOURS` | `12` | 管理页登录态有效期（小时）。滑动过期：每次操作自动续命 |
| `MANAGE_COOKIE_SECURE` | （空） | 设为 `1` 或 `true`，会话 Cookie 加 `Secure` 标志（强制 HTTPS 才传）。线上建议开 |
| `VISION_API_KEY` | 等于 `Z_AI_API_KEY` | 图片转文字用的视觉模型 key。不填则复用 `Z_AI_API_KEY` |
| `VISION_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | 视觉模型 API 地址（OpenAI 兼容） |
| `VISION_MODEL` | `glm-5v-turbo` | 视觉模型名。换成 GPT-4o 等也行 |
| `VISION_MAX_IMAGES_PER_REQUEST` | `20` | 单次请求最多转几张图 |

---

## 3. 启动

### 最简启动（docker run）

```bash
docker run -d --name gateway \
  -p 4001:4001 \
  -e DATABASE_URL="postgresql://user:pass@your-pg-host:5432/db" \
  -e GATEWAY_MASTER_KEY="<你生成的随机长串>" \
  -e ARK_API_KEY="ark-xxxxxxxx-xxxx-..." \
  -e CLAUDE_CODE_KEY="sk-xxxxxxxx" \
  -e Z_AI_API_KEY="your-zhipu-key" \
  -e UI_USERNAME="admin" \
  -e UI_PASSWORD="你的强密码" \
  -e PUBLIC_BASE_URL="https://your-gateway.example.com" \
  -e MANAGE_COOKIE_SECURE=1 \
  litellm-gateway
```

### PaaS（Railway/Render/Fly）

把仓库导入平台，指定根目录 `Dockerfile` 构建。环境变量在平台 UI 里配（上面的表）。
PaaS 会自动注入 `PORT`，镜像的 `HEALTHCHECK` 指令也供平台探活用。

> **端口**：PaaS 注入的 `PORT` 会覆盖默认的 4001。镜像内 `server.py` 读 `PORT` 环境变量决定监听端口，所以平台改端口无需改镜像。`EXPOSE 4001` 只是声明，PaaS 不受此限制。

### 用 `.env` 文件（自建主机）

```bash
docker run -d --name gateway --env-file .env -p 4001:4001 litellm-gateway
```

`.env` 格式见 [.env.example](.env.example)。

---

## 4. 健康检查

```bash
curl -sf http://localhost:4001/health/liveness
# 返回 200 即正常
```

镜像内置了 Docker `HEALTHCHECK`（每 30s 探一次 `/health/liveness`，启动后 40s 才开始探），PaaS 会读这个。

---

## 5. 管理页

部署后打开：

```
https://your-gateway.example.com/manage/
```

**第一次访问会跳到登录页**（`/manage/login`）。输入 `GATEWAY_MASTER_KEY` 登录，成功后签发 HttpOnly Cookie（12 小时有效，操作自动续期）。master key 只在登录时验一次，之后全程不出现在 URL 里。

管理页能做：
- **创建 key**：填用户名、勾后端（ark/claude/claude_1/claude_2/zai，可多选）、可选设预算上限和 RPM。创建后显示 key 明文 + Claude Code / OpenAI 客户端的配置代码块
- **查看 key 列表**：每个 key 显示后端 badge、用量、预算
- **查看模型映射**：每行点「N 条映射 ▾」展开，看「客户端发的模型名 → 实际路由到哪个后端模型」
- **编辑 key**：点「编辑」改后端（重建模型路由）或预算，秒级生效不重启
- **删除 key**
- **登录审计**：底部表格显示最近的登录记录（成功/失败、IP、时间），同时写容器日志和数据库 `manage_login_audit` 表

原生 LiteLLM Admin UI 在 `/ui`，用 `UI_USERNAME`/`UI_PASSWORD` 登录，功能更全（Logs 详情、guardrails、teams 等）但创建 key 时要手填模型别名，不如 `/manage/` 直观。

---

## 6. 客户端接入

创建 key 后，管理页会给出现成的配置：

**Claude Code**（`~/.claude/settings.json`）：
```jsonc
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://your-gateway.example.com",
    "ANTHROPIC_AUTH_TOKEN": "sk-创建的key"
  }
}
```

**OpenAI 兼容客户端**：
```
base_url = https://your-gateway.example.com/v1
api_key  = sk-创建的key
```
发 `model=claude` 或 `model=ark` 选后端（多后端 key 才需要）。

---

## 7. 常见问题

### Q：首次启动很慢（40-60 秒），正常吗？

正常。容器启动时 `server.py` 会先跑 `prisma db push` 给空库建表（LiteLLM 有 70+ 张表），迁移完才启动 uvicorn。看容器日志会有：
```
[server] 运行 prisma db push 建表/同步 schema...
[server] ✓ prisma db push 完成
[server] LiteLLM + manage 启动: host=0.0.0.0 port=4001 ...
```
后续重启时 db push 是幂等的（检测无变化跳过），会快很多。

### Q：管理页创建 key 报 500 / 「LiteLLM 创建失败」

多半是 `prisma db push` 没跑成功（db 还没就绪、或连接串不对）。查容器日志：
```bash
docker logs gateway 2>&1 | grep -E "server|prisma|manage_audit|LiteLLM_SpendLogs"
```
如果看到 `prisma db push 非零退出` 或 `relation "LiteLLM_VerificationToken" does not exist`，就是迁移没成功——检查 `DATABASE_URL` 是否正确、托管 PG 是否允许容器连接（防火墙/SSL 要求）。

### Q：端口怎么改？

PaaS 自动注入 `PORT` 环境变量，镜像会跟随。自建主机用 `-e PORT=8080 -p 8080:8080`。

### Q：`/manage/` 登录页一直报「管理密钥错误」

确认你传的 `GATEWAY_MASTER_KEY` 环境变量值和你在登录页输的一致。注意首尾不要有空格。如果忘了，看容器环境：`docker exec gateway printenv GATEWAY_MASTER_KEY`。

### Q：怎么吊销所有已登录会话？

改 `GATEWAY_MASTER_KEY` 环境变量重启。会话 Cookie 用 master key 做 HMAC 签名，密钥一变所有旧 Cookie 立即失效，所有人被踢下线。

### Q：托管 PG 要求 SSL 怎么办？

在 `DATABASE_URL` 连接串里加 `?sslmode=require`（或 `verify-full`）。LiteLLM 底层的 Prisma 支持。

### Q：线上要不要开 `ip_allowlist`？

`backends.yaml` 里默认配了 `ip_allowlist: [127.0.0.1]`，**线上必须改或去掉**——PaaS 的入站 IP 是平台代理（不固定），白名单会误拦自己。线上靠 HTTPS + master key + per-key 预算兜底足够。要限制来源 IP 的话，在 `backends.yaml` 加平台代理网段，重新构建镜像。

### Q：改了后端配置（加减后端/模型）怎么生效？

后端结构在构建期烤进镜像的 `config.yaml`。改 [backends.yaml](litellm/profiles/backends.yaml) 后要**重新构建镜像**再部署（后端结构是镜像的一部分，不是运行时可改的）。改 provider key 的值则不用重建——那是环境变量，平台 UI 改完重启即生效。

### Q：vision（图片转文字）不生效？

`needs_vision: true` 的后端（ark/zai）收到图片时，vision_hook 会调视觉模型转成文字。不生效通常是因为：
- `Z_AI_API_KEY` 没配（视觉模型默认用智谱，没 key 就跳过转图）
- 或自己配了 `VISION_API_KEY`/`VISION_BASE_URL`/`VISION_MODEL` 但值不对

查日志：`docker logs gateway 2>&1 | grep vision_hook`，会看到 `[vision_hook] 调用 glm-5v-turbo 描述图片...` 之类的输出。

---

## 8. 本地验证镜像（不影响线上）

用 `gw.sh` 脚本在本地跑单容器镜像验证，复用 compose 的 db、用 4002 端口，不碰线上 4001：

```bash
./gw.sh image up          # 构建镜像 + 启动单容器（4002 端口，复用 compose db）
./gw.sh image up --build  # 强制重建镜像后启动
./gw.sh image status      # 查看容器状态
./gw.sh image logs        # 跟踪日志
./gw.sh image shell       # 进容器排查
./gw.sh image down        # 停止并清理
```

脚本会等健康检查通过后返回。如果需要完整 compose 模式（db + litellm 一起启停，4001 端口）：

```bash
./gw.sh compose up / down / restart / status / logs
```
