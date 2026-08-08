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
| HTTPS（远程访问时） | ⬜ 未配置 | 需加反向代理 |

---

## 四、Master Key 说明

网关的 Master Key（`ARK_API_KEY`）是管理员级密钥，拥有全部权限。**不要**分发给终端用户。

终端用户使用通过 `./keys.sh new` 创建的虚拟 key（`sk-...`），这些 key 受预算、速率、模型限制约束。

Master Key 本身不受 IP 白名单限制（它走内部认证），但 UI 登录和 API 调用仍受 IP 白名单控制。
