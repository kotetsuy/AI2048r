#!/usr/bin/env bash
# 2048 × OpenClaw オフライン実況デモ 一括起動スクリプト。
# EarthTourGuide/start_all.sh の流儀を踏襲（tmux セッション + wait_http）。
#
# 起動順（各サービスは既に上がっていればスキップ＝idempotent）:
#   1. 2048 静的サーバ            :8009   (tmux)
#   2. VOICEVOX (docker)          :50021
#   3. llama-server (qwen3.6)     :8080   (tmux, -c 65536 --parallel 2)
#   4. three-vrm (VRM+背景+/speak):8000   (tmux, 同梱 AI2048/three-vrm)
#   5. 2048 表示用 Chrome (CDP)   :9222   (headed, ボットが操作/背景配信元)
#   6. OpenClaw gateway (docker)  :18789
#   7. VRM 表示(全画面)+背景配信  → start_phase2_display.sh (zundamon.html + bgcast)
#
# ホスト側プロセスは tmux セッション "ai2048" の別ウィンドウで走る:
#   tmux attach -t ai2048        (ログ閲覧)
#   ./stop_all.sh                (全停止)
set -uo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"
export DISPLAY="${DISPLAY:-:0}"

SESSION="ai2048"
# スクリプト自身の位置から解決（フォルダ名に依存しない）。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- llama（AIzunda/EarthTourGuide と共用。確定構成 -c 65536 --parallel 2）---
LLAMA_BIN="/home/$USER/llama.cpp/build/bin/llama-server"
QWEN_MODEL="/home/$USER/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
LLAMA_PORT="8080"
LLAMA_CTX="65536"
LLAMA_PARALLEL="2"
LLAMA_NGL="99"

VOICEVOX_CONTAINER="voicevox_engine"
VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-ubuntu20.04-latest"

GAME_DIR="/home/$USER/2048"
GAME_PORT="8009"
CDP_PORT="9222"
CDP_PROFILE="/tmp/chrome-cdp-2048"
COMPOSE_DIR="${ROOT}/openclaw-demo"

# gfx1151 (Ryzen AI Max+ 395) 向け ROCm env（llama 用）。
export HSA_OVERRIDE_GFX_VERSION="${HSA_OVERRIDE_GFX_VERSION:-11.5.1}"
export ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
export AMDGPU_TARGETS="${AMDGPU_TARGETS:-gfx1151}"
export LD_LIBRARY_PATH="/usr/local/lib:/opt/rocm/lib:/opt/rocm/lib/llvm/lib:${LD_LIBRARY_PATH:-}"

# ---- helpers ------------------------------------------------------------
log()  { printf '\033[1;34m[start]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[start]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[start]\033[0m %s\n' "$*" >&2; exit 1; }

up() { curl -sf -o /dev/null -m 2 "$1"; }

wait_http() {
    local name="$1" url="$2" timeout="${3:-120}" start now
    start=$(date +%s)
    log "waiting for ${name} (${url}) ..."
    while true; do
        if up "$url"; then log "  ${name} is up"; return 0; fi
        now=$(date +%s)
        (( now - start > timeout )) && die "${name} did not come up within ${timeout}s"
        sleep 2
    done
}

ensure_session() {
    tmux has-session -t "$SESSION" 2>/dev/null || \
        tmux new-session -d -s "$SESSION" -n root "echo '2048xOpenClaw demo'; exec sleep infinity"
}
new_window() {  # name cmd
    ensure_session
    tmux kill-window -t "${SESSION}:$1" 2>/dev/null || true
    tmux new-window -t "$SESSION" -n "$1"
    tmux send-keys -t "${SESSION}:$1" "$2" C-m
}

# ---- preflight ----------------------------------------------------------
command -v tmux          >/dev/null || die "tmux がありません"
command -v docker        >/dev/null || die "docker がありません"
command -v curl          >/dev/null || die "curl がありません"
command -v google-chrome >/dev/null || die "google-chrome がありません"
[[ -x "$LLAMA_BIN"  ]] || die "llama-server が見つかりません: $LLAMA_BIN"
[[ -f "$QWEN_MODEL" ]] || die "Qwen モデルが見つかりません: $QWEN_MODEL"
[[ -d "$GAME_DIR"   ]] || die "2048 ディレクトリがありません: $GAME_DIR"
[[ -d "${ROOT}/three-vrm" ]] || die "同梱 three-vrm がありません: ${ROOT}/three-vrm"

# ---- 1. 2048 静的サーバ :8009 ------------------------------------------
if up "http://localhost:${GAME_PORT}/"; then
    log "2048 サーバ(:${GAME_PORT}) は既に稼働"
else
    new_window 2048 "cd ${GAME_DIR} && python3 -m http.server ${GAME_PORT}"
    wait_http "2048 サーバ" "http://localhost:${GAME_PORT}/" 30
fi

# ---- 2. VOICEVOX :50021 -------------------------------------------------
if up "http://localhost:50021/version"; then
    log "VOICEVOX(:50021) は既に稼働"
else
    log "VOICEVOX コンテナを起動します"
    if docker ps -a --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
        docker start "$VOICEVOX_CONTAINER" >/dev/null
    else
        docker run -d --name "$VOICEVOX_CONTAINER" --restart unless-stopped \
            -p 50021:50021 "$VOICEVOX_IMAGE" >/dev/null
    fi
    wait_http "VOICEVOX" "http://localhost:50021/version" 60
fi

# ---- 3. llama-server :8080 ---------------------------------------------
if up "http://localhost:${LLAMA_PORT}/health"; then
    log "llama-server(:${LLAMA_PORT}) は既に稼働（共用llama）"
else
    LLAMA_CMD="HSA_OVERRIDE_GFX_VERSION=${HSA_OVERRIDE_GFX_VERSION} ROCM_PATH=${ROCM_PATH} \
HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES} AMDGPU_TARGETS=${AMDGPU_TARGETS} \
LD_LIBRARY_PATH=${LD_LIBRARY_PATH} \
${LLAMA_BIN} -m ${QWEN_MODEL} --host 127.0.0.1 --port ${LLAMA_PORT} \
-ngl ${LLAMA_NGL} -c ${LLAMA_CTX} --parallel ${LLAMA_PARALLEL} -fit off"
    new_window llama "$LLAMA_CMD"
    wait_http "llama-server" "http://localhost:${LLAMA_PORT}/health" 600
fi

# ---- 4. three-vrm :8000 (同梱) -----------------------------------------
if up "http://localhost:8000/status"; then
    log "three-vrm(:8000) は既に稼働"
else
    new_window three-vrm "cd ${ROOT}/three-vrm && DISPLAY=${DISPLAY} python3 server.py"
    wait_http "three-vrm" "http://localhost:8000/status" 30
fi

# ---- 5. 2048 表示用 Chrome (CDP) :9222 ---------------------------------
# ボットが操作し、背景配信(bgcast)の screencast 元にもなる headed Chrome。
if up "http://localhost:${CDP_PORT}/json/version"; then
    log "2048 Chrome(CDP :${CDP_PORT}) は既に稼働"
else
    rm -f "${CDP_PROFILE}/SingletonLock" 2>/dev/null || true
    DISPLAY="$DISPLAY" nohup google-chrome \
        --remote-debugging-port="${CDP_PORT}" --remote-debugging-address=0.0.0.0 \
        --remote-allow-origins=* --user-data-dir="${CDP_PROFILE}" \
        --no-first-run --no-default-browser-check \
        "http://localhost:${GAME_PORT}" >/tmp/chrome-cdp.log 2>&1 &
    disown
    wait_http "2048 Chrome(CDP)" "http://localhost:${CDP_PORT}/json/version" 30
fi

# ---- 6. OpenClaw gateway :18789 (docker) -------------------------------
if [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
    log "OpenClaw gateway を起動します"
    ( cd "$COMPOSE_DIR" && docker compose up -d openclaw-gateway ) || warn "gateway 起動に失敗"
    # healthy になるまで待つ（compose の healthcheck）
    gw_healthy=0
    for i in $(seq 1 30); do
        st="$(cd "$COMPOSE_DIR" && docker compose ps --format '{{.Status}}' openclaw-gateway 2>/dev/null)"
        echo "$st" | grep -q healthy && { log "  gateway healthy"; gw_healthy=1; break; }
        sleep 1
    done
    (( gw_healthy )) || warn "gateway が 30s で healthy になりませんでした（status: ${st:-不明}）。デモが回らない場合は docker compose logs を確認"
else
    warn "openclaw-demo/docker-compose.yml が無いため gateway をスキップ"
fi

# ---- 7. VRM 表示(全画面) + 背景配信(bgcast) -----------------------------
# 既存スクリプトを再利用（zundamon.html を全画面表示し、bgcast を自動起動）。
log "VRM 表示 + 背景配信を起動します（start_phase2_display.sh）"
"${ROOT}/start_phase2_display.sh" || warn "VRM 表示/背景配信の起動に問題（/tmp/chrome-vrm.log, /tmp/bgcast.log 参照）"

cat <<EOF

=========================================================================
 2048 × OpenClaw 実況デモ 起動完了

   2048           : http://localhost:${GAME_PORT}
   VOICEVOX       : http://localhost:50021/docs
   llama-server   : http://localhost:${LLAMA_PORT}/health
   three-vrm 表示 : http://localhost:8000/zundamon.html
   Chrome CDP     : http://localhost:${CDP_PORT}/json/version
   OpenClaw GW    : http://localhost:18789

 1ゲーム実況（例）:
   cd ${COMPOSE_DIR} && docker compose run --rm -T openclaw-cli \\
     agent --agent main --session-key demo\$(date +%s) \\
     --message "play2048 スキルで新規ゲームを始め、step→narrate で実況して。"

 tmux: tmux attach -t ${SESSION}     停止: ./stop_all.sh
=========================================================================
EOF
