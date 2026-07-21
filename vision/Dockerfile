# vision 插件：图片转文字 + 请求归一化的轻量前置层
FROM python:3.11-slim

WORKDIR /app

# 国内构建可加镜像加速：-i https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir uvicorn==0.30.6 starlette==0.37.2 httpx==0.27.2

COPY app.py .

EXPOSE 4001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4001"]
