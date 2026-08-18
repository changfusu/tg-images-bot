FROM python:3.11-slim

# OpenCV/onnxruntime 运行所需的系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyPI 镜像源，默认清华源（可用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖回官方源）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 先复制依赖并安装，利用构建缓存；代码变更不会导致依赖层重建
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 再复制代码（.dockerignore 已排除 data/、模型等运行时文件）
COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python", "main.py"]
