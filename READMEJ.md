# 2048 × OpenClaw オフライン実況デモ

> English: [`README.md`](README.md) / [`TECHNICAL.md`](TECHNICAL.md)

トレードショー向けの **完全オフライン** デモ。ローカルの 2048 を AI が自動プレイし、
VRM アバター「コテコ」（元気のいい女の子）が実況する。「AI が画面を見て考えて喋っている」絵で足を止めさせる。

- 手の決定は **expectimax（純 Python・CPU）** — 安定して 2048 に到達。
- 盤面は **localStorage の `gameState`** から読む（画像認識でなく誤差ゼロ）。
- 制御ループの中心は **OpenClaw（Docker）エージェント**。毎ターン Qwen3 がスキルを呼ぶ。
- 実況は **VOICEVOX**（ずんだもん声）→ **three-vrm**（VRM アバター）に同期。
- 2048 の盤面を背景にライブ配信し、その前でアバターが喋る **1 画面の絵**。

![アーキテクチャ](images/architecture.svg)

> 内部設計・実装の詳細は [`TECHNICALJ.md`](TECHNICALJ.md) を参照。

---

## 1. 動作環境

| 項目 | 値 |
|---|---|
| マシン | NucBox EVO X2 / Ryzen AI MAX+ 395 / gfx1151 / 48GB unified memory |
| OS | Ubuntu 24.04 |
| GPU | ROCm 7.2.x（`HSA_OVERRIDE_GFX_VERSION=11.5.1`） |
| 表示/音声 | xrdp + GNOME Remote Desktop、PipeWire → xrdp-sink |

LLM/VLM は gfx1151（ROCm）、OpenClaw 本体・expectimax・ブラウザ制御は CPU。

---

## 2. 前提（リポジトリ外で用意するもの）

このリポジトリはオーケストレーション一式（スクリプト・OpenClaw 設定・three-vrm 同梱版）を含むが、
以下の **大きいアセットは別途用意** する（`.gitignore` 対象 / ライセンス上同梱しない）。

| 必要物 | 既定パス | 入手方法 |
|---|---|---|
| 2048 ゲーム本体 | `~/2048` | `git clone https://github.com/gabrielecirulli/2048` |
| llama.cpp（ROCm ビルド） | `~/llama.cpp/build/bin/llama-server` | gfx1151 向けに ROCm 対応でビルド（`HSA_OVERRIDE_GFX_VERSION=11.5.1`） |
| Qwen3 モデル | `~/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` | GGUF を配置 |
| VRM アバター | `~/AIassistant/vroid/koteko.vrm` | VRM ファイルを配置 |
| VOICEVOX | Docker イメージ | `start_all.sh` が自動 pull/起動 |

必須コマンド: `docker`（+ compose v2）, `tmux`, `curl`, `google-chrome`, `python3`, `xrandr`。

---

## 3. セットアップ（git clone → 実行）

```bash
# 1) クローン
cd ~
git clone <このリポジトリ> AI2048r
cd AI2048r

# 2) 2048 ゲーム本体を取得（オフライン配信元）
git clone https://github.com/gabrielecirulli/2048 ~/2048

# 3) 背景配信(bgcast)用の venv を作成（playwright + aiohttp。Chromium 本体は不要）
python3 -m venv .venv
./.venv/bin/pip install playwright aiohttp
#   ※ connect_over_cdp はホストの Chrome を使うので `playwright install` は不要。

# 4) Compose の .env を用意（パスとトークンを含む。.env はリポジトリに含まれない）
cd openclaw-demo
STATE="$(pwd)/state"
{ echo "OPENCLAW_IMAGE=openclaw-2048:local";
  echo "OPENCLAW_CONFIG_DIR=${STATE}";
  echo "OPENCLAW_WORKSPACE_DIR=${STATE}/workspace";
  echo "OPENCLAW_GATEWAY_PORT=18789";
  echo "OPENCLAW_GATEWAY_BIND=lan";
  echo "OPENCLAW_TZ=Asia/Tokyo";
  echo "OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)"; } > .env

# 5) OpenClaw のコンテナ画像をビルド（公式画像 + Python + playwright）
docker compose build           # → openclaw-2048:local
cd ..
```

`openclaw-demo/.env` は **`OPENCLAW_GATEWAY_TOKEN`（シークレット）を含むため `.gitignore` 済み**で、
リポジトリには含まれない。テンプレートは [`openclaw-demo/.env.example`](openclaw-demo/.env.example) にあり、
手動で用意する場合は `cp .env.example .env` してパス（リポジトリの絶対パス）とトークンを書き換える。
上記 4) のワンライナーは現在地から `.env` を自動生成する。

---

## 4. 起動（これ一発）

```bash
cd ~/AI2048r
./start_all.sh
```

`start_all.sh` が以下を順に立ち上げる（既に上がっていればスキップ＝idempotent）:

1. 2048 静的サーバ `:8009`（tmux）
2. VOICEVOX（docker）`:50021`
3. llama-server `:8080`（`-c 65536 --parallel 2`、AIzunda/EarthTourGuide と共用）
4. three-vrm `:8000`（VRM 表示 + `/speak` + 背景中継）
5. 2048 表示用 Chrome（CDP `:9222`、headed）
6. OpenClaw gateway（docker）`:18789`
7. VRM 全画面表示 + 背景配信（`start_phase2_display.sh`）

ホスト側プロセスは tmux セッション `ai2048` で走る。ログは `tmux attach -t ai2048`、
Chrome/bgcast は `/tmp/chrome-*.log` `/tmp/bgcast.log`。

### 動作確認（1 ゲームだけ実況）

```bash
cd openclaw-demo
docker compose run --rm -T openclaw-cli \
  agent --agent main --session-key demo$(date +%s) \
  --message "play2048 スキルで新規ゲームを始め、step→narrate で実況して報告して。"
```

exit 0 かつホスト画面の盤面が変化し、アバターが喋れば OK。

---

## 5. 連続稼働（デモ本番）

```bash
cd ~/AI2048r
./demo_loop.sh
```

外側ループが毎回フレッシュな `--session-key` で OpenClaw エージェントを呼び、
「数手 `steps` → `narrate`（間引き）」→ 終局で勝敗演出 → `newgame` を繰り返す。

![制御フロー](images/control-flow.svg)

調整用 env:

| env | 既定 | 意味 |
|---|---|---|
| `DEMO_MOVES` | 8 | 1 セッションあたりの手数 |
| `DEMO_GAP` | 2 | セッション間の待ち（秒） |
| `DEMO_FRESH` | 1 | 開始時に newgame するか（0 で継続） |

**停止方法**:
- アバター画面の **「⏹ デモ停止」ボタン**（即時停止。進行中セッションも `docker stop`）。
- 端末で `Ctrl-C`（現セッション完了後に停止）。
- 起動 PID は `/tmp/demo_loop.pid` に記録される。

---

## 6. 停止

```bash
./stop_all.sh                 # デモ一式を停止
./stop_all.sh --keep-shared   # 共用の llama / VOICEVOX は残す（推奨）
```

`llama-server` と `VOICEVOX` は EarthTourGuide / AIzunda と共用なので、
共用環境では `--keep-shared` を使うこと。

---

## 7. ポート一覧

| サービス | ポート | 用途 |
|---|---|---|
| 2048 静的サーバ | 8009 | ゲーム配信 |
| three-vrm | 8000 | VRM 表示 / `/speak` / 背景中継 |
| llama-server | 8080 | Qwen3（実況テキスト生成・共用） |
| Chrome CDP | 9222 | ボット操作 / 背景 screencast 元 |
| OpenClaw gateway | 18789 | エージェント制御プレーン |
| VOICEVOX | 50021 | 音声合成 |

---

## 8. トラブルシュート

- **アバターが喋らない**: `curl localhost:8000/status` の `clients` が 0 → 表示 Chrome が `/ws` 未接続。
  `start_phase2_display.sh` を再実行。`/tmp/chrome-vrm.log` を確認。
- **背景に 2048 が出ない**: `/tmp/bgcast.log` に `/bg_ingest connected` が無い → bgcast 未起動。
  `.venv` に playwright+aiohttp があるか確認。`NO_BGCAST=1` で無効化も可能。
- **エージェントが overflow**: llama は `-c 65536 --parallel 2` で起動すること（per-slot 32768 が必要）。
- **CDP に繋がらない**: Chrome は `/tmp/chrome-cdp-2048` プロファイルで headed 起動が必要。
  起動前に `rm -f /tmp/chrome-cdp-2048/SingletonLock`。
- **`Cannot continue from message role: assistant`**: セッション `main` の使い回し →
  毎回ユニークな `--session-key` を渡す。

詳細な設計・既知のハマりどころは [`TECHNICALJ.md`](TECHNICALJ.md)。

---

## 9. ステータス

| フェーズ | 状態 |
|---|---|
| Phase 0（CDP 接続フィージビリティ） | ✅ 完了 |
| Phase 1（OpenClaw コンテナ化 + スキル + エージェント駆動） | ✅ 完了（`--parallel 2` + 小コンテキストで確定） |
| Phase 2（実況の実音声化: narrate→VOICEVOX→three-vrm） | ✅ 完了（VRM 表示 + テンポ同期まで実機検証） |
| Phase 3（磨き込み） | ✅ ほぼ完了（背景化 / 一括起動停止 / 連続稼働 / 勝敗演出 / 停止ボタン / 字幕 / 正面化） |

直近の 5 分間デモテストでは 4 セッション・失敗 0、自動リスタート・元気な女の子実況・テンポ同期を確認、
VRAM 24.93→25.06GB（リークなし）。**残るは長時間（1〜2 時間相当）連続稼働の耐久確認のみ**。
