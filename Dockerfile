# =============================================================================
# 单容器镜像：LiteLLM + 自建管理页（/manage）+ vision hook
# 适用于通用 PaaS（Railway / Render / Fly）。本地开发仍用 docker-compose。
#
# 构建：  docker build -t litellm-gateway .
# 运行：  docker run -p 4001:4001 \
#           -e DATABASE_URL=postgresql://... \
#           -e GATEWAY_MASTER_KEY=... \
#           -e ARK_API_KEY=... -e CLAUDE_CODE_KEY=... -e Z_AI_API_KEY=... \
#           litellm-gateway
# =============================================================================
FROM ghcr.io/berriai/litellm:main-stable

WORKDIR /app

# ---- 烤进镜像：后端结构（单一真相源）+ 生成器 + hook ----
COPY litellm/profiles/backends.yaml /app/backends.yaml
COPY litellm/profiles/gen_config.py /app/gen_config.py
COPY litellm/hooks/vision_hook.py /app/hooks/vision_hook.py

# ---- 烤进镜像：自建管理页 + 容器入口 ----
COPY manage/ /app/manage/
COPY server.py /app/server.py
COPY requirements-manage.txt /app/requirements-manage.txt

# 管理页依赖：jinja2/httpx/pyyaml 镜像已内置；psycopg2-binary 需补装（镜像无 pip，用 ensurepip 引导）
RUN python3 -m ensurepip >/dev/null 2>&1 \
    && python3 -m pip install --no-cache-dir psycopg2-binary \
    && python3 -c "import jinja2, httpx, yaml, psycopg2; print('manage deps OK')"

# 构建期生成 config.yaml（后端结构固化，密钥运行时从 env 读，不进镜像）
RUN python3 gen_config.py gen-config --backends /app/backends.yaml /app/config.yaml

# LiteLLM 在 startup lifecycle 用 CONFIG_FILE_PATH 定位配置
ENV CONFIG_FILE_PATH=/app/config.yaml
ENV PYTHONPATH=/app/hooks:/app
# PORT 由 PaaS 注入；默认 4001 与本地一致
ENV PORT=4001
EXPOSE 4001

# 健康检查（PaaS 用）
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=40s \
  CMD curl -sf http://localhost:${PORT}/health/liveness || exit 1

# 单进程入口：import litellm app → include 管理页 router → uvicorn
# 必须覆盖父镜像的 ENTRYPOINT（docker/prod_entrypoint.sh 会用 `litellm "$@"` 包装 CMD）
ENTRYPOINT []
CMD ["python3", "server.py"]
