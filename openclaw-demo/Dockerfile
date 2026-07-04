# OpenClaw + Python + Playwright の自前画像。
# play2048 スキルは `exec` で Python(Playwright) スクリプトを走らせるため、
# 公式画像(node:24-bookworm-slim ベース, Python無し)に Python と playwright を足す。
# 完全オフラインデモ前提のため、依存は起動時インストールせず画像に焼き込む。
# connect_over_cdp はブラウザ本体をDLしない（ホストのChromeをCDPで使う）ので
# `playwright install`(Chromium取得)は不要 → playwright パッケージのみで軽量。
ARG OPENCLAW_BASE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_BASE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
# bookworm は PEP 668 (externally-managed) のため --break-system-packages。
RUN python3 -m pip install --no-cache-dir --break-system-packages playwright

# 公式画像の実行ユーザ(node)へ戻す。
USER node
