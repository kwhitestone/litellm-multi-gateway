# 安全加固指南

> LiteLLM Multi-Gateway 的安全配置参考。涵盖 UI 操作、配置文件加固、IP 白名单三部分。

## 一、UI 操作（无需改配置文件）

登录 Admin UI：`http://127.0.0.1:4001/ui`

### 1.1 给每个虚拟 Key 设限制

在 **Keys** 页面，点击 key 进行编辑（或创建新 key 时设置）：

| 字段 | 说明 | 建议值 |
|---|---|---|
| **Max Budget ($)** | 该 key 的总消费上限，到顶自动拒绝 | 按实际用量设，如 $10/月 |
| **TPM Limit** | 每分钟最大 token 数 | 如 50000 |
| **RPM Limit** | 每分钟最大请求次数 | 如 30 |
| **Models** | 限制该 key 只能访问的模型 | 如只给 `claude-haiku-4-5-20251001`，不给 opus |
| **Expiry Date** | key 过期时间 | 按需设置，定期轮换 |

操作步骤：
1. 打开 `http://127.0.0.1:4001/ui`，用 `.env` 中的 `UI_USERNAME` / `UI_PASSWORD` 登录
2. 左侧菜单 **Keys** -> 找到要编辑的 key -> 点击编辑
3. 填入上述限制 -> 保存
4. 对每个 key 重复此操作

### 1.2 Team / User 级别预算（可选）

如果有多人使用，可创建 Team 和 User 来分组管理：
- **Teams** 页面 -> 创建团队 -> 设置团队总预算和 TPM/RPM
- **Users** 页面 -> 创建用户 -> 设置用户级预算
- 把 key 绑定到对应的 team/user

### 1.3 审计与监控

- **Logs** 页面：查看每个请求的 model、tokens、spend、完整 request/response
- **Usage** 页面：按 key/user/model 维度查看用量趋势，发现异常用量

---

## 二、配置文件加固（已完成）

### 2.1 Admin UI 强密码

**文件**：`.env`

```env
UI_USERNAME=admin
UI_PASSWORD=<随机生成的强密码>
```

> 如果 `.env` 中没有这两行，LiteLLM 会使用 `docker-compose.yml` 的默认值 `admin/admin`，非常危险。

**已配置**：2026-08-07 已将 UI 密码从默认 `admin/admin` 改为随机生成的强密码。

### 2.2 IP 白名单

**文件**：`litellm/profiles/backends.yaml` -> `general_settings.ip_allowlist`

```yaml
general_settings:
  ip_allowlist:
    - "127.0.0.1"            # 本机（默认场景）
    # - "192.168.1.0/24"     # 局域网网段（取消注释并按需修改）
    # - "203.0.113.5"        # 特定公网 IP（取消注释并填实际 IP）
```

**已配置**：2026-08-07 已加入 `ip_allowlist: ["127.0.0.1"]`，仅允许本机访问。

**开放远程访问的步骤**：
1. 编辑 `litellm/profiles/backends.yaml`，在 `ip_allowlist` 列表中添加目标 IP/网段
2. 编辑 `docker-compose.yml`，把端口绑定从 `127.0.0.1:4001:4001` 改为 `0.0.0.0:4001:4001`（或特定网卡 IP）
3. 重新生成配置并重启：
   ```bash
   ./keys.sh gen-config
   docker compose up -d
   ```
4. 验证：从白名单外的 IP 访问应返回 403

**注意**：开放远程访问时务必同时启用 HTTPS（通过反向代理如 Nginx/Caddy），否则流量明文传输。

### 2.3 修改 IP 白名单后重新生成配置

`ip_allowlist` 写在 `backends.yaml` 中，需要重新生成 `multi.yaml` 才生效：

```bash
./keys.sh gen-config          # 或 python3 litellm/profiles/gen_config.py gen-config
docker compose up -d          # 重启 litellm
```

---

## 三、安全现状总结

| 措施 | 状态 | 位置 |
|---|---|---|
| 端口绑定 127.0.0.1 | ✅ 已有 | `docker-compose.yml` |
| Postgres 不对外暴露 | ✅ 已有 | `docker-compose.yml` |
| `.env` 在 .gitignore | ✅ 已有 | `.gitignore` |
| Admin UI 强密码 | ✅ 已配置 | `.env` -> `UI_PASSWORD` |
| IP 白名单 | ✅ 已配置 | `backends.yaml` -> `general_settings.ip_allowlist` |
| Per-key 预算上限 | ⬜ 需在 UI 操作 | Admin UI -> Keys |
| Per-key 速率限制 | ⬜ 需在 UI 操作 | Admin UI -> Keys |
| Per-key 模型限制 | ⬜ 需在 UI 操作 | Admin UI -> Keys |
| HTTPS（远程访问时） | ⬜ 未配置 | 需加反向代理（PaaS 默认提供） |

---

## 四、线上部署（PaaS：Railway/Render/Fly）

线上用单容器镜像（`Dockerfile`），Postgres 用平台托管实例，key 管理走 `/manage/` 自建管理页（不依赖 CLI）。

### 4.1 必填环境变量（平台 UI 配置）

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 托管 Postgres 连接串（如 `postgresql://user:pass@host:5432/db`） |
| `GATEWAY_MASTER_KEY` | 网关管理密钥，**单独随机生成**（如 `sk-gw-xxx`），勿复用 provider key |
| `ARK_API_KEY` | 火山 ark coding plan token |
| `CLAUDE_CODE_KEY` | 公司 Claude 网关 key |
| `Z_AI_API_KEY` | 智谱 BigModel key（vision hook 视觉模型默认也用） |
| `UI_USERNAME` / `UI_PASSWORD` | Admin UI 登录凭据，密码务必强 |
| `CLAUDE_CODE_KEY_1` / `CLAUDE_CODE_KEY_2` | 可选，多 key 隔离用量（claude_1/claude_2 后端） |
| `PUBLIC_BASE_URL` | 可选，对外公开地址（管理页展示给客户端的 BASE_URL） |

### 4.2 HTTPS

PaaS（Railway/Render/Fly）默认提供 TLS 终止——平台给你一个 `https://xxx` 域名，到容器的流量平台已加密。**无需自配证书**。

直接用平台给的 HTTPS 域名作为客户端的 BASE_URL，不要用 http://。

### 4.3 IP 白名单（线上建议去掉）

线上 PaaS 部署建议**去掉** `ip_allowlist`（或设为允许全部），原因：
- PaaS 的入站 IP 不固定（平台代理转发），白名单可能误拦
- HTTPS + master key + per-key 预算已构成足够保护

如确实要限制特定来源 IP，在 `backends.yaml` 的 `ip_allowlist` 加上平台代理网段。

### 4.4 部署流程

```bash
# 1. 平台（如 Railway）导入仓库，自动用根目录 Dockerfile 构建单容器镜像
# 2. 平台 UI 配置 4.1 的环境变量
# 3. 平台分配 HTTPS 域名
# 4. 浏览器打开 https://<域名>/manage/?k=<GATEWAY_MASTER_KEY>
#    → 创建 key（选后端、设预算）→ 拿到 sk-... 客户端 key
# 5. 客户端用 https://<域名> 作 BASE_URL + 创建的 key 接入
```

### 4.5 本地验证镜像（不影响线上）

```bash
./scripts/verify-image.sh        # 用 4002 端口 + 隔离 db 跑完整验证，跑完自动清理
./scripts/verify-image.sh --keep # 保留容器排查
```

---

## 五、Master Key 说明

网关的 Master Key（`GATEWAY_MASTER_KEY`）是管理员级密钥，拥有全部权限。**不要**分发给终端用户。

终端用户使用通过 `./keys.sh new`（本地）或 `/manage/`（线上）创建的虚拟 key（`sk-...`），这些 key 受预算、速率、模型限制约束。

Master Key 本身不受 IP 白名单限制（它走内部认证），但 UI 登录和 API 调用仍受 IP 白名单控制。
