# 部署镜像。**不用 nixpacks 自动推断**：它对 pyproject 项目的做法是先只拷
# pyproject.toml 再 `pip install .`，而那时源码目录还没拷进来，必然失败
# （`error: package directory 'responder' does not exist`）。
# 一个十行的 Dockerfile 比跟自动推断较劲省事得多，而且构建内容一目了然。
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 先拷元数据再拷源码：改代码时这一层的依赖缓存还能命中。
COPY pyproject.toml README.md ./
COPY responder ./responder
COPY scripts ./scripts
RUN pip install --no-cache-dir .

# 端口由平台通过 PORT 传入（见 responder/app.py 的 serve()）。
CMD ["responder-api"]
