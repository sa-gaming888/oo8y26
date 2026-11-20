# syntax=docker/dockerfile:1
# 程式映像：FROM 基底映像（Dockerfile.base，base.yml 建），只放程式與資料。
# 程式只用標準函式庫，不 pip install 任何套件。
# 本機建：docker build -f Dockerfile.base -t oo8y26-base:local . && docker build --build-arg BASE=oo8y26-base:local -t oo8y26:local .

ARG BASE=ghcr.io/sa-gaming888/oo8y26-base:latest
FROM ${BASE}
COPY --chown=65532:65532 . /app
RUN ["python", "-m", "compileall", "-q", "/app/oo8y26"]
VOLUME ["/data"]
EXPOSE 8790
ENTRYPOINT ["python", "-X", "utf8", "-m", "oo8y26.container"]
