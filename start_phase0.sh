#!/usr/bin/env bash
# Phase 0 のホスト側サービスを起動する。
#   1. 2048 静的配信 :8009  （~/2048 を http.server で。:8000はEarthTourGuideのthree-vrmが使用）
#   2. Chrome を CDP有効・headed で :9222 起動し 2048 を表示
# その後 phase0_cdp_test.py を実行すれば CDP 接続を検証できる。
#
# 使い方:
#   ./start_phase0.sh          # サービス起動（フォアグラウンドで待機）
#   別ターミナルで: .venv/bin/python phase0_cdp_test.py
#
# 停止: Ctrl-C（このスクリプトが起動した子プロセスを後始末する）
set -euo pipefail

GAME_DIR="$HOME/2048"
PORT_HTTP=8009   # :8000 は EarthTourGuide の three-vrm が使用するため移動
PORT_CDP=9222
CHROME_PROFILE=/tmp/chrome-cdp-2048

pids=()
cleanup() {
  echo
  echo "停止中..."
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

# --- 1. 2048 静的配信 ---
if ss -ltn "( sport = :$PORT_HTTP )" | grep -q ":$PORT_HTTP"; then
  echo "[skip] :$PORT_HTTP は既に LISTEN 中"
else
  echo "[start] http.server :$PORT_HTTP  ($GAME_DIR)"
  ( cd "$GAME_DIR" && exec python3 -m http.server "$PORT_HTTP" ) >/tmp/2048-http.log 2>&1 &
  pids+=($!)
fi

# --- 2. Chrome (CDP, headed) ---
if ss -ltn "( sport = :$PORT_CDP )" | grep -q ":$PORT_CDP"; then
  echo "[skip] :$PORT_CDP は既に LISTEN 中（Chrome起動済み）"
else
  echo "[start] google-chrome --remote-debugging-port=$PORT_CDP (headed)"
  google-chrome \
    --remote-debugging-port="$PORT_CDP" \
    --remote-debugging-address=0.0.0.0 \
    --user-data-dir="$CHROME_PROFILE" \
    --no-first-run --no-default-browser-check \
    --start-maximized \
    "http://localhost:$PORT_HTTP" >/tmp/2048-chrome.log 2>&1 &
  pids+=($!)
fi

sleep 2
echo
echo "起動完了:"
echo "  2048    : http://localhost:$PORT_HTTP"
echo "  CDP     : http://localhost:$PORT_CDP/json/version"
echo
echo "別ターミナルで検証:  .venv/bin/python phase0_cdp_test.py"
echo "Ctrl-C で停止"
wait
