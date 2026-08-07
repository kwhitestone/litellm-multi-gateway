# AGENTS.md - Agent 排查指南

> 本文件供 AI agent（Claude Code 等）在本工程中排查 LiteLLM 及插件的请求链路问题时参考。
> 覆盖请求链路、常见根因模式、诊断方法和修复步骤。

## 请求链路

```
Claude Code
  -> 127.0.0.1:4001 (LiteLLM proxy, Docker)
    -> 后端 API (ark / claude / zai)
      -> [claude 后端] aicoding-gateway.ndaeweb.com:8443 -> Anthropic API
```

LiteLLM 是中间代理层。客户端的请求经过两层处理：

1. **proxy 层**（`/v1/messages`、`/v1/chat/completions`）— FastAPI 路由入口，执行 auth、pre_call_hook、路由分发
2. **handler 层** — Anthropic pass-through handler 或 chat/completions handler，构造发往后端的 HTTP 请求

## 关键组件

| 文件 | 作用 |
|---|---|
| `litellm/profiles/backends.yaml` | 后端配置单一真相源（人类编辑） |
| `litellm/profiles/multi.yaml` | 从 backends.yaml 生成的 LiteLLM 配置（勿手改） |
| `litellm/profiles/gen_config.py` | backends.yaml -> multi.yaml 生成器 + 映射解析器 |
| `litellm/hooks/vision_hook.py` | CustomLogger hook：按模型 needs_vision 转图/透传 + 协议归一化 |
| `keys.sh` | 虚拟 key 管理 CLI（创建/路由/删除 key） |
| `docker-compose.yml` | litellm + postgres 容器编排 |

## 排查方法论

### 第一步：看 LiteLLM 日志

```bash
# 实时跟踪
docker logs -f litellm-multi-gateway-litellm-1

# 看最近 50 行
docker logs litellm-multi-gateway-litellm-1 --tail 50

# 只看 vision_hook 输出
docker logs litellm-multi-gateway-litellm-1 2>&1 | grep vision_hook
```

关键信号：
- `[vision_hook] [pre_call/...]` — hook 被触发，看 model / needs_vision / 图片数 / 剥离块数
- `WARNING: transformation.py` — LiteLLM 在丢弃不支持的参数（如 thinking 对 glm-5.2）
- `POST /v1/messages?beta=true` — 客户端发的 beta 请求（Claude Code 的 skill/ToolSearch 依赖）

### 第二步：查数据库审计

LiteLLM 把请求/响应存进 postgres，可以直接查：

```bash
docker exec litellm-multi-gateway-db-1 psql -U litellm -d litellm -c \
  "SELECT request_id, model, api_key, spend, tokens_prompt, tokens_completion
   FROM LiteLLM_SpendLogs
   ORDER BY startTime DESC LIMIT 10;"
```

如果要查完整的 request body（确认 skill attachment / tools 是否被正确传递）：

```bash
docker exec litellm-multi-gateway-db-1 psql -U litellm -d litellm -c \
  "SELECT messages, response
   FROM LiteLLM_SpendLogs
   ORDER BY startTime DESC LIMIT 1;" | head -100
```

### 第三步：读 LiteLLM 源码定位

LiteLLM 容器内的源码路径：`/app/.venv/lib/python3.13/site-packages/litellm/`

关键源码文件（排查时常用 `docker exec ... cat/grep` 查看）：

| 文件 | 作用 |
|---|---|
| `proxy/anthropic_endpoints/endpoints.py` | `/v1/messages` 路由入口 |
| `proxy/common_request_processing.py` | 请求处理主逻辑（hook 执行、路由分发） |
| `proxy/litellm_pre_call_utils.py` | pre_call_hook 逻辑 + proxy_server_request 构造 + header 转发 |
| `llms/anthropic/experimental_pass_through/messages/handler.py` | Anthropic pass-through handler |
| `llms/custom_httpx/llm_http_handler.py` | HTTP 请求构造和发送（headers 合并在这里） |
| `anthropic_beta_headers_manager.py` | anthropic-beta header 过滤/映射 |
| `anthropic_beta_headers_config.json` | 各 provider 支持的 beta header 白名单 |

容器内 grep（BusyBox 不支持 `--include`，用 `-r` 配合文件名过滤）：

```bash
docker exec litellm-multi-gateway-litellm-1 \
  grep -rn "关键词" /app/.venv/lib/python3.13/site-packages/litellm/proxy/
```

## 常见根因模式

### 1. 客户端 header 未转发给后端

**症状：** Claude Code 的 beta 特性（skills、ToolSearch、context-1m 等）在后端不生效，但基础功能正常。

**根因：** LiteLLM 默认不转发客户端 headers 给后端 API。`forward_client_headers_to_llm_api` 默认 `False`。

**代码位置：** `proxy/litellm_pre_call_utils.py` 的 `add_litellm_data_for_backend_llm_call`：

```python
if general_settings and general_settings.get("forward_client_headers_to_llm_api") is True:
    _headers = LiteLLMProxyRequestSetup.add_headers_to_llm_call(headers, user_api_key_dict)
    if _headers != {}:
        data["headers"] = _headers
```

**修复：** 在 `backends.yaml` 的 `general_settings` 加 `forward_client_headers_to_llm_api: true`，然后：

```bash
./keys.sh gen-config && docker compose up -d
```

**注意：** 上游网关（如 aicoding-gateway）也必须透传 `anthropic-beta` header 给真正的 Anthropic API，否则只改 LiteLLM 不够。

### 2. vision_hook 对错误的请求链生效

**症状：** hook 在 `/v1/chat/completions` 上不生效，只在 `/v1/messages` 上生效（或反之）。

**根因：** LiteLLM 两条请求链的 hook 点不同：
- `async_pre_call_hook` — 所有 proxy 端点，路由**前**跑
- `async_pre_request_hook` — 仅 `/v1/messages`（anthropic pass-through），路由**后**跑

**排查：** 看 hook 日志中的 `[pre_call/...]` vs `[pre_request]` 标签。如果只看到一个，说明只走了一条链。

**修复：** 两个 hook 方法都要实现（vision_hook.py 已经这样做了）。

### 3. LiteLLM 丢弃后端不支持的参数

**症状：** 日志中出现 `Dropping adaptive thinking/effort for model=xxx`。

**根因：** LiteLLM 的 `drop_params` 机制会自动丢弃后端不支持的参数。`backends.yaml` 里 `drop_params: false` 时保留所有参数，但部分参数仍会被 AnthropicConfig 的 `_maybe_drop_speed_param` 等方法丢弃。

**排查：** 查日志中的 `WARNING: transformation.py` 行。

**影响：** 对 ark/zai（glm-5.2）等不支持的参数（thinking、effort）会被丢弃，不影响功能。但如果 `drop_params: true` 可能会丢弃更多必要参数。

### 4. beta header 被 LiteLLM 过滤掉

**症状：** 即使 `forward_client_headers_to_llm_api: true`，某些 beta 特性仍不生效。

**根因：** LiteLLM 有一个 beta header 白名单机制（`anthropic_beta_headers_config.json`）。不在白名单里的 header 会被 `update_headers_with_filtered_beta` 过滤掉。

**排查：**

```bash
# 查看白名单
docker exec litellm-multi-gateway-litellm-1 \
  cat /app/.venv/lib/python3.13/site-packages/litellm/anthropic_beta_headers_config.json | python3 -m json.tool
```

**修复：** 如果需要的 beta header 不在白名单，可能需要升级 LiteLLM 版本或手动修改白名单文件（升级后会被覆盖）。

### 5. cache_control.ttl 顺序冲突

**症状：** 请求报 `cache_control.ttl` 相关错误（如 1h 和 5m 顺序冲突）。

**根因：** Claude Code 发送的请求中 `cache_control` 带 `ttl` 字段，后端可能对 ttl 顺序有要求。

**修复：** vision_hook.py 的 `_strip_cache_ttl` 函数已经处理了这个问题——它会递归删除所有 `cache_control.ttl`。如果仍出错，检查 hook 是否被正确加载（看日志中是否有 `[vision_hook]` 输出）。

## 诊断流程清单

遇到 LiteLLM 或插件问题时，按以下顺序排查：

1. **确认 LiteLLM 在运行**
   ```bash
   docker compose ps
   curl -s http://127.0.0.1:4001/health/liveness
   ```

2. **看实时日志**
   ```bash
   docker logs -f litellm-multi-gateway-litellm-1 2>&1
   ```
   复现问题，看是否有 ERROR / WARNING。

3. **确认 config 生效**
   ```bash
   # 确认 multi.yaml 包含预期配置
   grep "关键词" litellm/profiles/multi.yaml
   # 确认容器里的 config 与本地一致
   docker exec litellm-multi-gateway-litellm-1 cat /app/config.yaml | grep "关键词"
   ```

4. **确认 hook 被加载**
   ```bash
   docker logs litellm-multi-gateway-litellm-1 2>&1 | grep -i "callback\|hook\|vision"
   ```

5. **查数据库审计** — 看 LiteLLM 实际收到和发出的请求内容
   ```bash
   docker exec litellm-multi-gateway-db-1 psql -U litellm -d litellm -c \
     "SELECT model, messages FROM LiteLLM_SpendLogs ORDER BY startTime DESC LIMIT 1;" | head -50
   ```

6. **读源码** — 如果日志和审计不足以定位，直接读 LiteLLM 容器内的源码（路径见上文）

7. **绕过代理直连测试** — 如果怀疑是代理链问题，临时绕过 LiteLLM 直连后端测试
   ```bash
   # 直连 Anthropic 官方 API（如果有官方 key）
   ANTHROPIC_API_KEY=sk-ant-xxx \
   ANTHROPIC_BASE_URL=https://api.anthropic.com \
   claude -p "测试" --max-turns 1
   ```

## 修改配置后的标准流程

```bash
# 1. 编辑 backends.yaml（唯一真相源）
vim litellm/profiles/backends.yaml

# 2. 重新生成 multi.yaml
./keys.sh gen-config

# 3. 确认生成结果
diff <(grep "关键词" litellm/profiles/multi.yaml) <(echo "预期值")

# 4. 重启 LiteLLM
docker compose up -d
# 如果 config.yaml 是只读挂载但容器没检测到变化：
docker compose restart litellm

# 5. 验证
docker logs litellm-multi-gateway-litellm-1 --tail 20
curl -s http://127.0.0.1:4001/health/liveness
```

## LiteLLM 版本信息

```bash
docker exec litellm-multi-gateway-litellm-1 pip show litellm 2>/dev/null | grep Version
```

当前使用 `ghcr.io/berriai/litellm:main-stable` 镜像。如需固定版本，修改 `docker-compose.yml` 中的 image tag。
