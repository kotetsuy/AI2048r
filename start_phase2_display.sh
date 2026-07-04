#!/usr/bin/env bash
# Phase 2 表示: VRM ずんだもん（three-vrm zundamon.html）をホスト画面に出す。
# 確定事項7「VRMずんだもん同居」。2048 ウィンドウ（ボット駆動）の横にタイル配置する。
#
# 前提（先に起動しておく）:
#   - VOICEVOX :50021（docker start voicevox_engine）
#   - three-vrm :8000（cd ~/AI2048/three-vrm && DISPLAY=:10.0 python3 server.py）
#   - 2048 :8009 / Chrome CDP :9222（start_phase0.sh 等）
#
# このスクリプトは three-vrm 表示ページ専用 Chrome を 1 つ起動する。
#   - --app モードで zundamon.html を開き、/ws で /speak の実況を受信。
#   - --autoplay-policy=no-user-gesture-required で AudioContext を自動再生
#     （クリックなしで喋れるように）。音声はブラウザ→PipeWire→xrdp-sink。
#   - 既定でウィンドウ表示（全画面ではない）。画面中央に収まるサイズで開く。
#     env で上書き可: VRM_X / VRM_Y / VRM_W / VRM_H, DISPLAY, VRM_URL
#     全画面にしたい場合は VRM_FULLSCREEN=1 を指定する。
#
# 2048 ウィンドウを左半分に置きたい場合（任意・CDP Chrome を貼り替えるなら）:
#   google-chrome --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 \
#     --remote-allow-origins=* --user-data-dir=/tmp/chrome-cdp-2048 \
#     --window-position=0,0 --window-size=$((SW/2)),$SH http://localhost:8009
set -euo pipefail

export DISPLAY="${DISPLAY:-:10.0}"
VRM_URL="${VRM_URL:-http://localhost:8000/zundamon.html}"
PROFILE="/tmp/chrome-vrm-2048"

# three-vrm（このリポジトリ同梱版 AI2048/three-vrm）が応答しなければ自動起動する。
THREE_VRM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/three-vrm"
if ! curl -sf -o /dev/null "http://localhost:8000/status"; then
  if [ ! -f "$THREE_VRM_DIR/server.py" ]; then
    echo "ERROR: $THREE_VRM_DIR/server.py が見つかりません。" >&2
    exit 1
  fi
  echo "three-vrm(:8000) 未起動 → $THREE_VRM_DIR から起動します"
  ( cd "$THREE_VRM_DIR" && DISPLAY="$DISPLAY" setsid nohup python3 server.py \
      >/tmp/three-vrm.log 2>&1 </dev/null & )
  for i in $(seq 1 20); do
    curl -sf -o /dev/null "http://localhost:8000/status" 2>/dev/null && break
    sleep 1
  done
  if ! curl -sf -o /dev/null "http://localhost:8000/status"; then
    echo "ERROR: three-vrm を起動できませんでした。/tmp/three-vrm.log を確認してください。" >&2
    exit 1
  fi
fi

# 画面サイズ → 既定はウィンドウ表示（画面の約 75% を中央に配置）
read SW SH < <(xrandr 2>/dev/null | grep -oP '\d+x\d+(?=\s+\d+\.\d+\*)' | head -1 | tr 'x' ' ')
SW="${SW:-1024}"; SH="${SH:-768}"
VRM_W="${VRM_W:-$((SW * 3 / 4))}"
VRM_H="${VRM_H:-$((SH * 3 / 4))}"
VRM_X="${VRM_X:-$(((SW - VRM_W) / 2))}"
VRM_Y="${VRM_Y:-$(((SH - VRM_H) / 2))}"
echo "画面 ${SW}x${SH} / VRM ウィンドウ pos ${VRM_X},${VRM_Y} size ${VRM_W}x${VRM_H}（ウィンドウ）"

# 既存の表示用 Chrome を落としてから起動（多重起動防止）
pkill -f "$PROFILE" 2>/dev/null || true
rm -f "$PROFILE/SingletonLock" 2>/dev/null || true
sleep 0.5

# キャッシュバスター: zundamon.html を編集しても確実に最新版を読ませる。
case "$VRM_URL" in
  *\?*) OPEN_URL="${VRM_URL}&_=$(date +%s)" ;;
  *)    OPEN_URL="${VRM_URL}?_=$(date +%s)" ;;
esac

# 全画面にしたい場合のみ VRM_FULLSCREEN=1。既定はウィンドウ表示。
FULLSCREEN_FLAG=""
[ "${VRM_FULLSCREEN:-0}" = "1" ] && FULLSCREEN_FLAG="--start-fullscreen"

nohup google-chrome \
  --user-data-dir="$PROFILE" \
  --autoplay-policy=no-user-gesture-required \
  --no-first-run --no-default-browser-check \
  --disk-cache-size=1 --disable-application-cache \
  $FULLSCREEN_FLAG --window-position="${VRM_X},${VRM_Y}" --window-size="${VRM_W},${VRM_H}" \
  --app="$OPEN_URL" >/tmp/chrome-vrm.log 2>&1 &
disown

# /ws 接続（clients>=1）を待つ
connected=0
for i in $(seq 1 25); do
  c="$(curl -s http://localhost:8000/status 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("clients",0))' 2>/dev/null || echo 0)"
  if [ "${c:-0}" -ge 1 ] 2>/dev/null; then
    echo "OK: three-vrm clients=$c（VRM 表示ページが接続）。narrate で喋るのだ。"
    connected=1
    break
  fi
  sleep 1
done
[ "$connected" = 1 ] || { echo "WARN: VRM ページが /ws に接続しませんでした。/tmp/chrome-vrm.log を確認してください。" >&2; exit 1; }

# --- 背景ライブ配信 (2048 を背景に / EarthTourGuide 方式) ---
# play2048_bgcast.py が 2048 Chrome(:9222) を CDP screencast し three-vrm /bg_ingest へ送る。
# 既に動いていれば二重起動しない。NO_BGCAST=1 でスキップ可。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${NO_BGCAST:-0}" != "1" ]; then
  if pgrep -f "play2048_bgcast.py" >/dev/null 2>&1; then
    echo "  背景配信(bgcast)は既に稼働中。"
  elif ! curl -sf -o /dev/null "http://localhost:9222/json/version" 2>/dev/null; then
    echo "  WARN: 2048 Chrome(:9222) が見つからないため背景配信(bgcast)はスキップ。" >&2
  else
    # playwright+aiohttp が使える python を選ぶ（自前 venv 優先、無ければ EarthTourGuide のを流用）。
    BGCAST_PY=""
    for cand in "$ROOT/.venv/bin/python" "$HOME/EarthTourGuide/earth-controller/.venv/bin/python"; do
      if [ -x "$cand" ] && "$cand" -c "from playwright.async_api import async_playwright; import aiohttp" >/dev/null 2>&1; then
        BGCAST_PY="$cand"; break
      fi
    done
    if [ -z "$BGCAST_PY" ]; then
      echo "  WARN: playwright+aiohttp のある python が無く背景配信(bgcast)をスキップ。" >&2
      echo "        例: cd $ROOT && uv venv && uv pip install playwright aiohttp" >&2
    else
      DISPLAY="$DISPLAY" setsid nohup "$BGCAST_PY" "$ROOT/play2048_bgcast.py" \
        >/tmp/bgcast.log 2>&1 </dev/null &
      disown
      # 背景フレームが届くまで待つ（/status の bg_clients は購読者数。フレーム到達は last_frame で見る）
      for i in $(seq 1 15); do
        pgrep -f "play2048_bgcast.py" >/dev/null 2>&1 && grep -q "/bg_ingest connected" /tmp/bgcast.log 2>/dev/null && break
        sleep 1
      done
      if grep -q "/bg_ingest connected" /tmp/bgcast.log 2>/dev/null; then
        echo "OK: 背景配信(bgcast)稼働。2048 盤面が背景に流れるのだ（$BGCAST_PY）。"
      else
        echo "  WARN: 背景配信(bgcast)の起動を確認できず。/tmp/bgcast.log を確認。" >&2
      fi
    fi
  fi
fi
exit 0
