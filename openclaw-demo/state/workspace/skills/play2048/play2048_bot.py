#!/usr/bin/env python3
"""
2048 自動プレイbot（expectimax）

前提:
  1. gabrielecirulli/2048 をローカルに置いて静的配信しておく
       git clone https://github.com/gabrielecirulli/2048
       cd 2048 && python -m http.server 8000     # → http://localhost:8000
  2. 依存:
       pip install playwright
       playwright install chromium

設計:
  - 盤面は localStorage の "gameState"（GameManagerが毎ターン保存する完全な状態）
    から JSON で読む。DOMパースも画像認識も不要、誤差ゼロ。
  - 手の決定は純Pythonのexpectimax（CPUで完結。GPUはLLM/VLM側に空けられる）。
  - on_move() が実況フック。ここに moondream2 のコメント生成 → llama-server →
    VOICEVOX(ずんだもん) を繋ぐ。今はコンソール出力のスタブ。

注意:
  - ゲームオーバー時、GameManagerは localStorage の gameState を「消す」ので、
    敗北判定は .game-message のクラスで見る。勝利(2048)は state["won"] で見る。
"""

import json
import time
# playwright は main()（スタンドアロン実行）でのみ必要。expectimax ソルバ群は純粋関数なので、
# choose_move 等を import するだけの利用側（play2048_cdp.py の narrate 等）に playwright を強制しない。

# ---- 設定 -------------------------------------------------------------
URL = "http://localhost:8009"     # 2048をローカル配信したURL（:8000はEarthTourGuideのthree-vrmが使用）
MOVE_DELAY = 0.4                  # 手と手の間隔(秒)。デモで見せるなら0.3〜0.6が見やすい
STOP_AT_2048 = True               # 2048到達で止めて勝利演出。Falseなら続行
KEY = {0: "ArrowUp", 1: "ArrowRight", 2: "ArrowDown", 3: "ArrowLeft"}
DIR_JA = {0: "上", 1: "右", 2: "下", 3: "左"}

# 蛇行(snake)重み: 最大タイルを左上に集めて単調性を保つ古典的ヒューリスティック
WEIGHT = [
    [4**15, 4**14, 4**13, 4**12],
    [4**8,  4**9,  4**10, 4**11],
    [4**7,  4**6,  4**5,  4**4],
    [4**0,  4**1,  4**2,  4**3],
]
EMPTY_BONUS = 4**13               # 空きマスの価値。詰まり防止。要調整

# ---- 2048 ロジック ----------------------------------------------------
def _compress_merge_left(line):
    """1列(行)を左(添字0方向)へ詰めてマージ"""
    nums = [v for v in line if v != 0]
    out, i = [], 0
    while i < len(nums):
        if i + 1 < len(nums) and nums[i] == nums[i + 1]:
            out.append(nums[i] * 2)
            i += 2
        else:
            out.append(nums[i])
            i += 1
    out += [0] * (4 - len(out))
    return out


def move_board(board, direction):
    """direction: 0=上,1=右,2=下,3=左。(新board, 変化したか) を返す"""
    if direction == 3:      # 左
        nb = [_compress_merge_left(r) for r in board]
    elif direction == 1:    # 右
        nb = [_compress_merge_left(r[::-1])[::-1] for r in board]
    elif direction == 0:    # 上
        cols = [[board[r][c] for r in range(4)] for c in range(4)]
        m = [_compress_merge_left(col) for col in cols]
        nb = [[m[c][r] for c in range(4)] for r in range(4)]
    else:                   # 下
        cols = [[board[r][c] for r in range(4)] for c in range(4)]
        m = [_compress_merge_left(col[::-1])[::-1] for col in cols]
        nb = [[m[c][r] for c in range(4)] for r in range(4)]
    return nb, (nb != board)


def evaluate(board):
    score = 0
    for r in range(4):
        for c in range(4):
            v = board[r][c]
            if v == 0:
                score += EMPTY_BONUS
            else:
                score += v * WEIGHT[r][c]
    return score


def expectimax(board, depth, is_chance):
    if depth <= 0:
        return evaluate(board)
    if is_chance:
        empties = [(r, c) for r in range(4) for c in range(4) if board[r][c] == 0]
        if not empties:
            return evaluate(board)
        total = 0.0
        for (r, c) in empties:
            for value, prob in ((2, 0.9), (4, 0.1)):
                board[r][c] = value          # in-placeでdeepcopyを回避
                total += prob * expectimax(board, depth - 1, False)
                board[r][c] = 0
        return total / len(empties)
    # プレイヤーノード(max)
    best = None
    for d in range(4):
        nb, changed = move_board(board, d)
        if not changed:
            continue
        v = expectimax(nb, depth - 1, True)
        if best is None or v > best:
            best = v
    return best if best is not None else evaluate(board)


def _depth_for(board):
    """空きが少ないほど分岐が減るので深く読める"""
    empties = sum(1 for r in range(4) for c in range(4) if board[r][c] == 0)
    if empties >= 6:
        return 3
    if empties >= 3:
        return 4
    return 5


def choose_move(board):
    depth = _depth_for(board)
    best_dir, best_val = None, None
    for d in range(4):
        nb, changed = move_board(board, d)
        if not changed:
            continue
        v = expectimax(nb, depth - 1, True)
        if best_val is None or v > best_val:
            best_val, best_dir = v, d
    return best_dir


# ---- ブラウザ駆動 -----------------------------------------------------
def read_board(page):
    """localStorageのgameStateから盤面を取得。cells[x][y]: x=列, y=行"""
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


def is_lost(page):
    cls = page.evaluate(
        "() => { const m = document.querySelector('.game-message');"
        " return m ? m.className : ''; }"
    )
    return "game-over" in cls


def on_move(board, direction, score, won=False):
    """実況フック。
    ここで盤面スクショ→moondream2でコメント生成→llama-server→VOICEVOX(コテコ/ずんだもん声)。
    現在はコンソール出力のスタブ。
    """
    if won:
        line = f"やったー！2048作れたよ！スコアは{score}だよっ！"
    elif direction is not None:
        line = f"{DIR_JA[direction]}に動かすよっ（スコア{score}）"
    else:
        return
    print(line)
    # 例) requests.post("http://localhost:50021/audio_query", ...) でVOICEVOX再生
    # 例) moondream2に board のスクショを渡して情景描写を生成し line に混ぜる


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        page = browser.new_page(no_viewport=True)
        page.goto(URL)

        # 毎回まっさらな新規ゲームから
        page.evaluate("() => window.localStorage.clear()")
        page.reload()
        page.wait_for_selector(".tile-container")
        page.click("body")  # キー入力のフォーカス確保

        moves = 0
        while True:
            if is_lost(page):
                print(f"ゲームオーバー（{moves}手）")
                break
            board, state = read_board(page)
            if board is None:
                time.sleep(0.05)
                continue
            if state and state.get("won") and STOP_AT_2048:
                print(f"🎉 2048達成！（{moves}手）")
                on_move(board, None, state["score"], won=True)
                break
            d = choose_move(board)
            if d is None:
                print("詰み: 動かせる手がありません")
                break
            page.keyboard.press(KEY[d])
            moves += 1
            on_move(board, d, state["score"] if state else 0)
            time.sleep(MOVE_DELAY)

        time.sleep(3)
        browser.close()


if __name__ == "__main__":
    main()
