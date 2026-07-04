#!/usr/bin/env python3
"""
2048 自動プレイ — CDP接続版 CLI（OpenClaw play2048 スキルの同梱スクリプト）

設計（2026-06-07 レビューで案B採用 / openclaw_phase1_findings.md）:
  OpenClaw エージェントが毎ターン制御ループの中心に立つ。SKILL.md は本CLIの
  サブコマンドを `exec` で1ターンずつ呼ぶ。手の決定は expectimax 固定（確定事項5）で
  Python 側に閉じる。状態はブラウザの localStorage に持つので各コマンドはステートレス
  （毎回 connect_over_cdp して都度読む）。

サブコマンド:
  read           盤面と状態を JSON で返す（board/score/won/over/max_tile/empty）
  solve          expectimax で次の手（方向）を JSON で返す
  press <dir>    指定方向(0=上,1=右,2=下,3=左)に着手
  step           read+solve+press を1回に束ねる（毎ターンの主役。エージェント往復を抑制）
  newgame        まっさらな新規ゲームを開始
  narrate        実況フック（three-vrm /speak → VOICEVOX → VRMずんだもん同居）
  play           モノリシックな自動プレイループ（保険。テンポ破綻時の退避用）

出力は機械可読性のため stdout に1行 JSON。人間用ログは stderr。

前提（start_phase0.sh で起動済み）:
  2048 配信  : http://localhost:8000
  Chrome CDP : http://localhost:9222 (headed)

実行例:
  .venv/bin/python play2048_cdp.py step
  .venv/bin/python play2048_cdp.py play --delay 0.4
"""

import argparse
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

# playwright は browser を触るコマンド(read/solve/press/step/newgame/play)でのみ必要。
# narrate は TTS だけなので import を遅延し、playwright 無し環境でも narrate を動かせる。

# expectimax ソルバは既存 bot から流用（確定事項5: 手の決定は expectimax 固定）
from play2048_bot import choose_move

# ホスト指定は env で上書き可能。
#  - ホスト直実行: 既定の localhost でよい。
#  - OpenClaw コンテナ内 exec: host.docker.internal を指す
#      （PLAY2048_CDP_URL=http://host.docker.internal:9222 等）。
CDP_URL = os.getenv("PLAY2048_CDP_URL", "http://localhost:9222")
# :8000 は EarthTourGuide の three-vrm が使用するため 2048 は :8009。
GAME_URL = os.getenv("PLAY2048_GAME_URL", "http://localhost:8009")
# Phase 2: 実況は EarthTourGuide の three-vrm /speak（:8000）へ送り、
# VOICEVOX 合成 + VRM ずんだもん（同居）に喋らせる。確定事項7=「VRMずんだもん同居」。
THREE_VRM_URL = os.getenv("PLAY2048_VRM_URL", "http://localhost:8000")
VOICEVOX_SPEAKER = int(os.getenv("PLAY2048_SPEAKER_ID", "3"))  # 3=ずんだもん ノーマル
# テンポ同期: narrate は /speak が返す音声長(duration_sec)ぶん待ってから返る。
# → エージェントの step→narrate ループが「喋り終わってから次手」になり、実況が被らない。
NARRATE_WAIT = os.getenv("PLAY2048_NARRATE_WAIT", "1") not in ("0", "false", "False", "")
NARRATE_WAIT_FACTOR = float(os.getenv("PLAY2048_NARRATE_WAIT_FACTOR", "1.0"))
NARRATE_MAX_WAIT = float(os.getenv("PLAY2048_NARRATE_MAX_WAIT", "8.0"))  # 暴走防止の上限(秒)
MOVE_DELAY = float(os.getenv("PLAY2048_MOVE_DELAY", "0.4"))  # 着手間隔(秒)。env で短縮可
STOP_AT_2048 = True
KEY = {0: "ArrowUp", 1: "ArrowRight", 2: "ArrowDown", 3: "ArrowLeft"}
DIR_JA = {0: "上", 1: "右", 2: "下", 3: "左"}


def log(*a):
    """人間向けログは stderr へ（stdout は JSON 専用に保つ）。"""
    print(*a, file=sys.stderr)


def emit(obj):
    """エージェントが食う1行 JSON を stdout へ。"""
    print(json.dumps(obj, ensure_ascii=False))


# ---- CDP接続 ----------------------------------------------------------
def _cdp_endpoint(url):
    """CDPエンドポイントのホスト名をIPに解決して返す。
    Chrome の DevTools は DNS-rebinding 対策で Host ヘッダがIP/localhost以外だと
    /json/version を拒否する。コンテナから host.docker.internal で繋ぐと名前のままでは
    弾かれるため、事前にIPへ解決して Host をIPにする。"""
    parts = urlsplit(url)
    host = parts.hostname
    if host in (None, "localhost", "127.0.0.1") or _is_ip(host):
        return url
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return url
    netloc = ip if parts.port is None else f"{ip}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _is_ip(host):
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


@contextmanager
def game_page():
    """connect_over_cdp して 2048 ページを yield する。ブラウザは閉じない。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(_cdp_endpoint(CDP_URL))
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for pg in ctx.pages:
            if GAME_URL in pg.url:
                page = pg
                break
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(GAME_URL)
        yield page


# ---- 盤面読取（play2048_bot.py から流用）-----------------------------
def parse_board(state):
    cells = state["grid"]["cells"]
    board = [[0] * 4 for _ in range(4)]
    for x, col in enumerate(cells):
        for y, cell in enumerate(col):
            if cell:
                board[y][x] = cell["value"]
    return board


def is_lost(page):
    cls = page.evaluate(
        "() => { const m = document.querySelector('.game-message');"
        " return m ? m.className : ''; }"
    )
    return "game-over" in cls


def game_status(page):
    """現在の盤面と状態をまとめて返す。
    ゲームオーバー時 GameManager は gameState を消すので、その場合は over=True。"""
    raw = page.evaluate("() => window.localStorage.getItem('gameState')")
    if not raw:
        # gameState 無し = 新規前 or ゲームオーバーで消去済み
        return {"board": None, "score": 0, "won": False,
                "over": is_lost(page), "max_tile": 0, "empty": 0}
    # 注: ゲームオーバーでない限り board は非None。コンテナ越しの高レイテンシでは
    # gameState が一瞬欠ける（None）ことがあるため、呼び出し側は board=None を
    # 過渡状態として扱い settle_status() で読み直すこと。
    state = json.loads(raw)
    board = parse_board(state)
    flat = [v for row in board for v in row]
    return {
        "board": board,
        "score": state.get("score", 0),
        "won": bool(state.get("won")),
        "over": is_lost(page),
        "max_tile": max(flat),
        "empty": sum(1 for v in flat if v == 0),
    }


def settle_status(page, tries=20, interval=0.05):
    """board が確定する（非None）か over になるまで読み直す。
    gameState の一瞬の欠落（特にコンテナ越しの高レイテンシ）を吸収する。"""
    st = game_status(page)
    while st["board"] is None and not st["over"] and tries > 0:
        time.sleep(interval)
        st = game_status(page)
        tries -= 1
    return st


def fmt_board(board):
    if board is None:
        return "(no board)"
    return "\n".join(
        " ".join(f"{v:4d}" if v else "   ." for v in row) for row in board
    )


# ---- 着手 -------------------------------------------------------------
def do_press(page, direction):
    page.keyboard.press(KEY[direction])


def do_newgame(page):
    page.evaluate("() => window.localStorage.clear()")
    page.reload()
    page.wait_for_function(
        "() => window.localStorage.getItem('gameState') !== null", timeout=10000
    )
    page.click("body")  # キー入力フォーカス確保
    time.sleep(0.2)


# ---- サブコマンド -----------------------------------------------------
def cmd_read(args):
    with game_page() as page:
        st = settle_status(page)
        log(fmt_board(st["board"]))
        emit(st)


def cmd_solve(args):
    with game_page() as page:
        st = settle_status(page)
        if st["board"] is None or st["over"]:
            emit({"direction": None, "reason": "no-board-or-over"})
            return
        d = choose_move(st["board"])
        if d is None:
            emit({"direction": None, "reason": "stuck"})
            return
        emit({"direction": d, "key": KEY[d], "dir_ja": DIR_JA[d]})


def cmd_press(args):
    with game_page() as page:
        do_press(page, args.direction)
        emit({"pressed": args.direction, "key": KEY[args.direction],
              "dir_ja": DIR_JA[args.direction]})


def cmd_step(args):
    """毎ターンの主役: read+solve+press を1回に束ね、何が起きたかを返す。"""
    with game_page() as page:
        st = settle_status(page)
        if st["over"]:
            emit({"event": "over", "score": st["score"], "max_tile": st["max_tile"]})
            return
        if st["won"] and STOP_AT_2048:
            emit({"event": "won", "score": st["score"], "max_tile": st["max_tile"]})
            return
        if st["board"] is None:
            emit({"event": "wait"})   # 過渡的に盤面欠落。エージェントは step を再試行。
            return
        d = choose_move(st["board"])
        if d is None:
            emit({"event": "stuck", "score": st["score"], "max_tile": st["max_tile"]})
            return
        do_press(page, d)
        log(f"{DIR_JA[d]} へ着手（score {st['score']}, max {st['max_tile']}）")
        emit({"event": "move", "direction": d, "key": KEY[d], "dir_ja": DIR_JA[d],
              "score": st["score"], "max_tile": st["max_tile"],
              "empty": st["empty"], "board": st["board"]})


def cmd_steps(args):
    """最速モード: 1回の CDP セッションで最大 count 手を一気に着手し、結果サマリを返す。
    エージェントの LLM 往復を手ごとに発生させず、数手まとめて進めて実況を間引くための主役。
    途中で won/over/stuck/wait になったらそこまでの結果で止めて返す。"""
    count = max(1, args.count)
    delay = args.delay if args.delay is not None else MOVE_DELAY
    made = 0
    last_dir = None
    with game_page() as page:
        for _ in range(count):
            st = settle_status(page)
            if st["over"]:
                emit({"event": "over", "moves": made,
                      "score": st["score"], "max_tile": st["max_tile"]})
                return
            if st["won"] and STOP_AT_2048:
                emit({"event": "won", "moves": made,
                      "score": st["score"], "max_tile": st["max_tile"]})
                return
            if st["board"] is None:
                if made == 0:
                    emit({"event": "wait", "moves": 0})  # 過渡。再試行。
                    return
                break  # ここまでの分を返す
            d = choose_move(st["board"])
            if d is None:
                emit({"event": "stuck", "moves": made,
                      "score": st["score"], "max_tile": st["max_tile"]})
                return
            do_press(page, d)
            made += 1
            last_dir = DIR_JA[d]
            time.sleep(delay)
        st = settle_status(page)  # バッチ後の最新状態を読み直して返す
        log(f"{made}手まとめて着手（score {st['score']}, max {st['max_tile']}）")
        emit({"event": "move", "moves": made, "dir_ja": last_dir,
              "score": st["score"], "max_tile": st["max_tile"],
              "empty": st["empty"], "board": st["board"]})


def cmd_newgame(args):
    with game_page() as page:
        do_newgame(page)
        st = game_status(page)
        log("新規ゲーム開始")
        emit({"event": "newgame", "board": st["board"]})


def speak_via_vrm(text, speaker_id=None, timeout=20):
    """three-vrm /speak（:8000）へ実況テキストを送る。
    VOICEVOX 合成 + VRM ずんだもん（同居）への WS 配信はサーバ側で行う。
    依存を増やさないため urllib のみ使用。成功時 server の JSON を返す。"""
    import urllib.request
    import urllib.error

    if speaker_id is None:
        speaker_id = VOICEVOX_SPEAKER
    payload = json.dumps({"text": text, "speaker_id": speaker_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{THREE_VRM_URL}/speak",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_narrate(args):
    """実況フック（Phase 2）。テキストを three-vrm /speak へ送り、VOICEVOX 合成 +
    VRM ずんだもん（同居）に喋らせる。確定事項7=「VRMずんだもん同居」。
    text 省略時は盤面状態から短い一言を生成する。"""
    text = args.text
    if not text:
        with game_page() as page:
            st = game_status(page)
        if st["over"]:
            text = "あ〜、ゲームオーバーだよ……"
        elif st["won"]:
            text = f"やったー！2048作れたよ！スコアは{st['score']}だよっ！"
        else:
            text = f"最大タイルは{st['max_tile']}だよっ（スコア{st['score']}）"

    spoken = False
    err = None
    duration = 0.0
    waited = 0.0
    if not args.no_tts:
        try:
            result = speak_via_vrm(text, speaker_id=args.speaker)
            spoken = bool(result.get("ok", True))
            duration = float(result.get("duration_sec", 0.0) or 0.0)
            log(f"[narrate] /speak OK ({duration:.2f}s): {text}")
            # テンポ同期: 喋り終わるまで待つ（--no-wait で無効化）。
            do_wait = NARRATE_WAIT and not args.no_wait
            if do_wait and duration > 0:
                waited = min(duration * NARRATE_WAIT_FACTOR, NARRATE_MAX_WAIT)
                time.sleep(waited)
        except Exception as e:  # noqa: BLE001 — 喋れなくてもループは止めない
            err = str(e)
            log(f"[narrate] /speak 失敗（{THREE_VRM_URL}）: {err} — テキストのみ続行")
    else:
        log(f"[narrate stub] {text}")

    emit({"event": "narrate", "text": text, "spoken": spoken,
          "duration_sec": round(duration, 3), "waited_sec": round(waited, 3),
          **({"error": err} if err else {})})


def cmd_play(args):
    """保険: モノリシックな自動プレイループ。テンポ破綻時の退避用。"""
    delay = args.delay
    with game_page() as page:
        if args.newgame:
            do_newgame(page)
        moves = 0
        while True:
            st = settle_status(page)
            if st["over"]:
                log(f"ゲームオーバー（{moves}手, 最大 {st['max_tile']}）")
                emit({"result": "lost", "moves": moves, "max_tile": st["max_tile"]})
                return
            if st["won"] and STOP_AT_2048:
                log(f"2048達成（{moves}手）")
                emit({"result": "won", "moves": moves, "max_tile": st["max_tile"],
                      "score": st["score"]})
                return
            if st["board"] is None:
                time.sleep(0.05)
                continue
            d = choose_move(st["board"])
            if d is None:
                emit({"result": "stuck", "moves": moves, "max_tile": st["max_tile"]})
                return
            do_press(page, d)
            moves += 1
            time.sleep(delay)


def build_parser():
    p = argparse.ArgumentParser(description="2048 CDP play CLI (OpenClaw play2048 skill)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read", help="盤面と状態をJSONで返す").set_defaults(func=cmd_read)
    sub.add_parser("solve", help="expectimaxで次の手を返す").set_defaults(func=cmd_solve)
    pr = sub.add_parser("press", help="指定方向に着手")
    pr.add_argument("direction", type=int, choices=[0, 1, 2, 3],
                    help="0=上 1=右 2=下 3=左")
    pr.set_defaults(func=cmd_press)
    sub.add_parser("step", help="read+solve+pressを1回に束ねる").set_defaults(func=cmd_step)
    sp = sub.add_parser("steps", help="最速: 複数手を一気に着手しサマリを返す（実況間引き用）")
    sp.add_argument("--count", type=int, default=4, help="まとめて進める手数（既定4）")
    sp.add_argument("--delay", type=float, default=None, help="着手間隔(秒)。既定はMOVE_DELAY")
    sp.set_defaults(func=cmd_steps)
    sub.add_parser("newgame", help="新規ゲーム開始").set_defaults(func=cmd_newgame)
    nr = sub.add_parser("narrate", help="実況（three-vrm /speak で VRMずんだもんに喋らせる）")
    nr.add_argument("--text", default=None, help="喋らせるテキスト（省略時は盤面から生成）")
    nr.add_argument("--no-tts", action="store_true", help="TTS送信せずテキストのみ（スタブ動作）")
    nr.add_argument("--no-wait", action="store_true", help="音声長ぶんの待機をしない（テンポ同期オフ）")
    nr.add_argument("--speaker", type=int, default=None,
                    help="VOICEVOX 話者ID（演出用。既定3=ずんだもんノーマル。1=あまあま/7=ツンツン等）")
    nr.set_defaults(func=cmd_narrate)
    pl = sub.add_parser("play", help="モノリシック自動プレイ（保険）")
    pl.add_argument("--delay", type=float, default=MOVE_DELAY, help="手の間隔(秒)")
    pl.add_argument("--newgame", action="store_true", help="開始前に新規ゲーム")
    pl.set_defaults(func=cmd_play)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
