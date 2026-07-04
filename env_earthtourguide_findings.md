# 既存スタック点検結果 — ~/EarthTourGuide（本デモが共用・流用するベース）

点検日: 2026-06-07。**本デモのベースは `~/EarthTourGuide`**（ユーザ指示で AIassistant から変更）。
EarthTourGuide は AIassistant の `llama.cpp` / `qwen3.6` / `ttllm` / `voicevox` / `whisperX-rocm` を
symlink で流用しつつ、独自の `three-vrm` / `earth-bridge` / `earth-controller` / `tour` を持つ。
起動: `~/EarthTourGuide/start_all.sh`（tmux セッション `earthtour`）/ 停止: `stop_all.sh`。

## 現在の稼働状況（点検時）
- 稼働中: Chrome CDP :9222（本デモPhase0）, 2048 http.server :8000（本デモ）
- **EarthTourGuide スタックは停止中**（llama/VOICEVOX/ttllm/earth-bridge/tour/three-vrm すべて未起動）。

## サービスとポート（start_all.sh より）
| サービス | ポート | 実体 | 備考 |
|---|---|---|---|
| VOICEVOX | 50021 | docker `voicevox_engine`（cpu-ubuntu20.04-latest, `--restart unless-stopped`） | ずんだもん speaker_id=3 |
| llama-server | 8080 | llama.cpp + `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` | **`--parallel 2` 付き**（= `-np 2`）✅ |
| ttllm | 8001 | `ttllm/server.py`（WhisperX↔llama） | llama `/v1/chat/completions` を `enable_thinking:False` |
| earth-bridge | 8002 | フレーム中継。`/control` で flyto 等 | Earth 操作系 |
| earth-controller | (専用ポート無し) | Earth 操作 + screencast。**headed Chrome を `DISPLAY=:10.0`** に出し bridge へフレーム供給 | Playwright で headed Chrome を既に運用 |
| three-vrm | 8000 | zundamon.html 配信 + TTS再生 | **2048 と :8000 衝突**⚠️ |
| tour | 8003 | ツアー進行（`/tour/start` 等） | |

## 流用ポイント: TTS 再生（narrate の接続先 / Phase 2）
EarthTourGuide 版 three-vrm も同じ: `POST http://localhost:8000/speak {"text":"...","speaker_id":3}`
→ VOICEVOX `/audio_query`+`/synthesis` → WebSocket broadcast → ブラウザ AudioContext 再生 + VRM 口パク。
llama 口調整形は ttllm が thinking 無効で実施済み（流用可）。

## 確定事項7（`-np 2`+）について → **EarthTourGuide では解消済み** ✅
start_all.sh は `--parallel 2`（`LLAMA_PARALLEL=2`）で llama を起動。
音声コマンドの行き先抽出(短い /chat)とナレーション生成を同時処理する設計。
→ AIassistant 版にあった「`-np` 無し」問題はこのベースでは発生しない。
   本デモの実況 LLM 呼び出しも同じ llama を共用でき、直列化しない。

## ✅ 解決済み: ポート :8000 衝突 → **2048 を :8009 へ移動**（2026-06-07 決定）
- 決定: **2048 を :8009 に逃がす**。three-vrm は :8000 のまま。EarthTourGuide スタックは無改変で併用可。
- 適用済みの変更（2箇所＋付随）:
  - `play2048_cdp.py` `GAME_URL` → `http://localhost:8009`
  - `start_phase0.sh` `PORT_HTTP` → `8009`
  - 付随: `phase0_cdp_test.py` `GAME_URL`、`play2048_bot.py` `URL` も 8009 に統一。
- 2048 配信先: `http://localhost:8009`（`~/2048` を `python3 -m http.server 8009`）。
- 関連: narrate 再生方式（three-vrm /speak でVRM口パク vs VOICEVOX直叩き+ホスト再生）は
  「絵」の決定と連動（Phase 2 で確定。現状は保留）。

## Phase 1（OpenClaw コンテナ）への影響
- コンテナ→ホスト到達先: llama `host.docker.internal:8080` / VOICEVOX `:50021` /
  2048 `:8000`(or 移動先) / Chrome CDP `:9222`。
- Phase 1 単体（実況なし1ゲーム完走）は llama/VOICEVOX 不要、CDP:9222 と 2048 配信のみで成立。
- 注意: earth-controller も headed Chrome を `DISPLAY=:10.0` で動かす。本デモの観客向け
  Chrome(CDP:9222) は別インスタンス。2つの headed Chrome / DISPLAY の住み分けに注意。

## three-vrm を AI2048 へ vendoring（Phase 2 / 2026-06-07）
当初 `~/EarthTourGuide/three-vrm/server.py` を直接改変したが、**共有スタックを汚さない**ため
方針変更: **three-vrm フォルダ一式を `~/AI2048/three-vrm/` にコピー（vendoring）し、デモは自前コピーを使う**。
- コピー元: `~/EarthTourGuide/three-vrm`（2.4M。TalkingHead/libs 含む）→ コピー先 `~/AI2048/three-vrm`。
- **EarthTourGuide 側は `git checkout` で pristine に復帰**（duration_sec 改変は取り消し済み。共有スタックは無改変）。
- 自前コピー `~/AI2048/three-vrm/server.py` には改変を保持:
  - `_wav_duration_sec()` 追加＋ `/speak` レスポンスに **`duration_sec`**（テンポ同期用。narrate が喋り終わるまで待つ）。
- 起動: `cd ~/AI2048/three-vrm && python3 server.py`（:8000）。`start_phase2_display.sh` が未起動なら自動起動する。
- 注意: `VRM_DIR=~/AIassistant/vroid`（koteko.vrm）と `IMAGES_DIR=~/AIzunda/images` は絶対パスのまま共有。
- narrate の「絵」は **VRMずんだもん同居**に確定（確定事項7）。表示は `http://localhost:8000/zundamon.html` を専用 Chrome で開く。
