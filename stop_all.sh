#!/usr/bin/env bash
# 2048 × OpenClaw 実況デモ 停止スクリプト。
#
#   ./stop_all.sh                  → デモ一式を停止（gateway/Chrome/bgcast/three-vrm/2048/llama/VOICEVOX）
#   ./stop_all.sh --keep-voicevox  → VOICEVOX は残す（他スタックと共用のため）
#   ./stop_all.sh --keep-llama     → llama-server は残す（AIzunda/EarthTourGuide と共用のため）
#   ./stop_all.sh --keep-shared    → VOICEVOX と llama の両方を残す
#
# 注意: llama-server と VOICEVOX は EarthTourGuide/AIzunda と共用。これらも動いていると
#       困る場合のみ停止する。共用環境では --keep-shared を推奨。
set -uo pipefail

SESSION="ai2048"
ROOT="/home/$USER/AI2048"
COMPOSE_DIR="${ROOT}/openclaw-demo"
VOICEVOX_CONTAINER="voicevox_engine"
CDP_PROFILE="/tmp/chrome-cdp-2048"
VRM_PROFILE="/tmp/chrome-vrm-2048"

KEEP_VOICEVOX=0
KEEP_LLAMA=0
for arg in "$@"; do
    case "$arg" in
        --keep-voicevox) KEEP_VOICEVOX=1 ;;
        --keep-llama)    KEEP_LLAMA=1 ;;
        --keep-shared)   KEEP_VOICEVOX=1; KEEP_LLAMA=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\033[1;34m[stop]\033[0m %s\n' "$*"; }

kill_pat() {  # 説明 パターン
    local desc="$1" pat="$2" pids
    pids=$(pgrep -f "$pat" || true)
    if [[ -n "${pids}" ]]; then
        log "停止: ${desc} (pid=${pids//$'\n'/,})"
        # shellcheck disable=SC2086
        kill ${pids} 2>/dev/null || true
        sleep 1
        pids=$(pgrep -f "$pat" || true)
        # shellcheck disable=SC2086
        [[ -n "${pids}" ]] && kill -9 ${pids} 2>/dev/null || true
    fi
}

# ポートを LISTEN しているプロセスを止める（相対パス起動で pgrep 不一致になる three-vrm 等に有効）。
kill_port() {  # 説明 ポート
    local desc="$1" port="$2" pids
    pids=$(ss -ltnpH "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)
    if [[ -n "${pids}" ]]; then
        log "停止: ${desc} (:${port} pid=$(echo ${pids} | tr ' ' ,))"
        # shellcheck disable=SC2086
        kill ${pids} 2>/dev/null || true
        sleep 1
        pids=$(ss -ltnpH "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)
        # shellcheck disable=SC2086
        [[ -n "${pids}" ]] && kill -9 ${pids} 2>/dev/null || true
    fi
}

# ---- 1. OpenClaw gateway (docker) --------------------------------------
if [[ -f "${COMPOSE_DIR}/docker-compose.yml" ]]; then
    log "OpenClaw gateway を停止します (docker compose down)"
    ( cd "$COMPOSE_DIR" && docker compose down ) >/dev/null 2>&1 || true
fi

# ---- 2. 背景配信ブリッジ bgcast ----------------------------------------
kill_pat "背景配信(bgcast)" "play2048_bgcast.py"

# ---- 3. 表示用 Chrome / 2048 CDP Chrome（プロファイル指定で限定）-------
# デモ専用プロファイルの Chrome のみ閉じる（ユーザの他 Chrome は触らない）。
kill_pat "VRM 表示 Chrome" "$VRM_PROFILE"
kill_pat "2048 CDP Chrome" "$CDP_PROFILE"

# ---- 4. tmux セッション（2048サーバ/llama/three-vrm のウィンドウ）------
if tmux has-session -t "$SESSION" 2>/dev/null; then
    if (( KEEP_LLAMA == 1 )); then
        # llama を残す場合はセッションは消さず、llama 以外のウィンドウだけ閉じる。
        for w in 2048 three-vrm root; do
            tmux kill-window -t "${SESSION}:${w}" 2>/dev/null || true
        done
        log "tmux: llama ウィンドウを残し、他を停止"
    else
        log "tmux セッション ${SESSION} を終了します"
        tmux kill-session -t "$SESSION" 2>/dev/null || true
    fi
else
    log "tmux セッション ${SESSION} は起動していません"
fi

# ---- 5. 取りこぼしプロセス（tmux 外で起動していた場合の保険）-----------
# three-vrm は `python3 server.py`(相対パス)で起動するため、ポート指定で確実に止める。
kill_port "2048 静的サーバ" "${GAME_PORT:-8009}"
kill_port "three-vrm"       "8000"
if (( KEEP_LLAMA == 0 )); then
    kill_pat "llama-server" "llama.cpp/build/bin/llama-server"
else
    log "llama-server は残します (--keep-llama)"
fi

# ---- 6. VOICEVOX docker ------------------------------------------------
if (( KEEP_VOICEVOX == 0 )); then
    if docker ps --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
        log "VOICEVOX コンテナを停止します"
        docker stop "$VOICEVOX_CONTAINER" >/dev/null 2>&1 || true
    fi
else
    log "VOICEVOX は残します (--keep-voicevox)"
fi

log "停止完了"
