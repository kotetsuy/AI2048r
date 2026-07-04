#!/usr/bin/env python3
"""
Phase 0 — CDP接続フィージビリティ確認

目的（CLAUDE.md §10 Phase 0）:
  ホストで起動した Chrome に connect_over_cdp で接続し、
    1. localStorage の gameState を読めるか
    2. 矢印キー送出で盤面が動くか
  を検証する。これが通れば既存 bot の launch() を connect_over_cdp() に
  差し替えるだけで OpenClaw コンテナ側からブラウザを駆動できる。

前提（このスクリプトを動かす前にホスト側で起動しておくもの）:
  1. 2048 をローカル配信:
       cd ~/2048 && python3 -m http.server 8000
  2. Chrome を CDP 有効・headed で起動:
       google-chrome --remote-debugging-port=9222 \
         --user-data-dir=/tmp/chrome-cdp-2048 http://localhost:8000

実行:
  .venv/bin/python phase0_cdp_test.py

start_services.sh が 1,2 を面倒見るので、通常はそちら経由で起動する。
"""

import json
import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
GAME_URL = "http://localhost:8009"   # :8000 は EarthTourGuide の three-vrm が使用するため移動
KEY = {0: "ArrowUp", 1: "ArrowRight", 2: "ArrowDown", 3: "ArrowLeft"}
DIR_JA = {0: "上", 1: "右", 2: "下", 3: "左"}


def read_board(page):
    """localStorageのgameStateから盤面を取得（play2048_bot.py から流用）。"""
    raw = page.evaluate("() => window.localStorage.getItem('gameState')")
    if not raw:
        return None, None
    state = json.loads(raw)
    cells = state["grid"]["cells"]
    board = [[0] * 4 for _ in range(4)]
    for x, col in enumerate(cells):
        for y, cell in enumerate(col):
            if cell:
                board[y][x] = cell["value"]
    return board, state


def fmt(board):
    return "\n".join(
        " ".join(f"{v:4d}" if v else "   ." for v in row) for row in board
    )


def get_game_page(browser):
    """既存コンテキストから 2048 のページを探す。無ければ開く。
    connect_over_cdp では browser.new_page() は既存ブラウザに新規タブを作る。
    観客に見せる窓を増やさないよう、まず既存ページを再利用する。
    """
    contexts = browser.contexts
    if not contexts:
        # CDP接続時は通常デフォルトコンテキストが1つある
        ctx = browser.new_context()
    else:
        ctx = contexts[0]

    for page in ctx.pages:
        if GAME_URL in page.url:
            return page
    # 2048 が開いていなければ既存ページを流用して遷移、無ければ新規
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(GAME_URL)
    return page


def main():
    results = {}
    with sync_playwright() as p:
        # --- (1) CDP接続 ---
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            results["connect_over_cdp"] = True
            print(f"[OK] connect_over_cdp({CDP_URL})  version={browser.version}")
        except Exception as e:
            print(f"[NG] connect_over_cdp 失敗: {e}")
            print("  → ホストで chrome --remote-debugging-port=9222 を起動済みか確認")
            sys.exit(1)

        page = get_game_page(browser)
        print(f"[OK] target page: {page.url}")

        # まっさらな新規ゲームから（既存 bot と同じ初期化）
        page.evaluate("() => window.localStorage.clear()")
        page.reload()
        # .tile-container はタイルが絶対配置でコンテナが0サイズになり Playwright が
        # hidden と判定するため、可視性ではなく実データ源(localStorage)の出現を待つ。
        page.wait_for_function(
            "() => window.localStorage.getItem('gameState') !== null",
            timeout=10000,
        )
        page.click("body")  # キー入力フォーカス確保
        time.sleep(0.3)

        # --- (2) localStorage 読取 ---
        board, state = read_board(page)
        if board is not None:
            results["read_localStorage"] = True
            nonzero = sum(1 for r in board for v in r if v)
            print(f"[OK] localStorage gameState 読取（初期タイル{nonzero}枚）")
            print(fmt(board))
        else:
            results["read_localStorage"] = False
            print("[NG] gameState を読めない")

        # --- (3) 矢印キー送出で盤面が変わるか ---
        changed_any = False
        before = json.dumps(board)
        for d in range(4):  # 上右下左を順に試し、どれかで変化すればOK
            page.keyboard.press(KEY[d])
            time.sleep(0.25)
            nb, _ = read_board(page)
            if nb is not None and json.dumps(nb) != before:
                changed_any = True
                print(f"[OK] キー送出 {KEY[d]}（{DIR_JA[d]}）で盤面が変化")
                print(fmt(nb))
                break
        results["keypress_moves_board"] = changed_any
        if not changed_any:
            print("[NG] どの矢印キーでも盤面が変化しなかった")

        # connect_over_cdp はホストのブラウザを閉じない（close しない）

    print("\n=== Phase 0 結果 ===")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(results.get(k) for k in
             ("connect_over_cdp", "read_localStorage", "keypress_moves_board"))
    print("=== " + ("ALL PASS — CDP経由でbotを駆動可能" if ok else "一部FAIL") + " ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
