# OpenClaw 調査結果 — CLAUDE.md §12 の回答（Phase 1 準備）

調査日: 2026-06-07 / 公式: https://docs.openclaw.ai, https://github.com/openclaw/openclaw

OpenClaw は実在の OSS。**local-first な AI アシスタント基盤**で、Gateway（単一コントロール
プレーン）＋ Docker サンドボックス＋スキル機構を持つ。model-agnostic（OpenAI / Anthropic /
Gemini / Ollama / 任意の OpenAI互換API）。Node 24 推奨。設定は JSON5 の
`~/.openclaw/openclaw.json`（コンテナ内は `/home/node/.openclaw`）。

> 注意: 「OpenClaw」には同名の Captain Claw ゲーム再実装プロジェクトも存在するが**別物**。
> 本デモが指すのは openclaw/openclaw（AIアシスタント基盤）の方。

---

## §12-① プロバイダ設定で任意 base_url（llama-server）を指定できるか → **可能**

`models.providers.<id>` に OpenAI互換プロバイダを定義できる。`api: "openai-completions"`
が llama-server / vLLM / SGLang など `/v1/chat/completions` 系の自己ホストに対応。
ダミーAPIキーで可。モデル参照は `"<provider-id>/<model-id>"`。

```json5
{
  models: {
    mode: "merge",
    providers: {
      "llama-host": {                         // 任意のID
        baseUrl: "http://host.docker.internal:8080/v1",  // ホストの llama-server
        apiKey: "dummy-key",
        api: "openai-completions",
        models: [
          { id: "qwen3", name: "Qwen3.6-35B-A3B",
            reasoning: false, input: ["text"],
            contextWindow: 128000, maxTokens: 32000 },
        ],
      },
    },
  },
  agents: {
    defaults: {
      model: { primary: "llama-host/qwen3" }, // provider/model 形式
    },
  },
}
```

→ CLAUDE.md §7 の「custom / openai-compatible プロバイダ + ダミーキー」がそのまま実現可能。
   コンテナからホストの llama-server へは `host.docker.internal:8080`（§7参照）。

## §12-② スキル/カスタムツールの実装インターフェース → **SKILL.md（markdown）+ 既存 `exec` ツールで同梱スクリプト実行**

**重要な設計含意**: OpenClaw のスキルは「独自の typed tool を JS/TS で登録する」ものでは**ない**。
スキル = `SKILL.md`（YAMLフロントマター＋markdown本文の指示書）。本文でエージェントに
「いつ・どの組み込みツールを呼ぶか」を教える。組み込みツールに `exec`（シェル実行）があり、
**同梱スクリプトは `exec` で実行する**。同梱ファイルは `{baseDir}` で参照。

- 配置: `~/.openclaw/workspace/skills/<skill>/SKILL.md`（+ `scripts/` 等の同梱物）
- フロントマター必須: `name`（小文字英数とハイフン）, `description`（1行・160字未満）
- 本文例: 「`exec` ツールで `{baseDir}/scripts/run.sh` を実行せよ」

→ **本デモへの落とし込み（決定: 2026-06-07 レビューで案B採用）**:
  OpenClaw を毎ターンの制御ループの中心に置く（受け入れ基準§11 / §8「agentic に見せる」に忠実）。
  `play2048` スキルは `SKILL.md` ＋ 同梱 Python CLI。SKILL.md がエージェントに
  「1ターンずつツールを `exec` で呼び、ゲーム終了まで繰り返せ」と指示する。
  既存 `play2048_cdp.py` の関数群を **CLI サブコマンド化**して提供:
    - `read`    → localStorage から盤面JSON（+score/won/over）を返す
    - `solve`   → expectimax で方向を返す（確定事項5維持、手の決定はPython）
    - `press D` → 矢印キー送出
    - `narrate` → 実況（Phase 2: screenshot→moondream2→llama-server→VOICEVOX）
    - `newgame` → 新規ゲーム
  各サブコマンドは毎回 connect_over_cdp し、状態はブラウザの localStorage に持つ（ステートレスでOK）。

  **△対策（テンポ/安定性: 案B採用時の最大リスク = ターン毎LLM往復）**:
  - 1017手規模では「read/solve/press/narrate を毎ターン個別 exec」だとエージェント(LLM)の
    往復が爆発する。→ **read+solve+press を1コマンド `step` に束ね**、エージェントの
    毎ターン呼び出しを「`step`（盤面+着手を返す）」＋「`narrate`」の最大2回に抑える。
    これでも OpenClaw は毎ターン関与し「中心」性は満たす。粒度は実機テンポを見て調整。
  - `narrate` の LLM/VLM はキュー化し、喋り終わる前に次手で被らないよう同期（§9）。
  - フォールバック: テンポが破綻する場合のみ、デモ中は Python 側ループに退避できる
    モノリシック実行(`play`)も CLI に残し、保険にする。
- 補足: `exec` で Python+Playwright を動かすには**サンドボックス画像にそれらが入っている**必要あり。
  ベース画像は `node:24-bookworm-slim` で Python 同梱なし → 後述の §12-④で対応。

## §12-③ 隔離ネットワークの allowlist でホストサービスを許可する設定 → **キー特定済み**

Docker サンドボックスは**既定で network なし**。ホスト到達には:

```json5
{
  agents: {
    defaults: {
      sandbox: {
        mode: "all",            // or "non-main"。非mainセッションをサンドボックス化
        docker: { network: "bridge" },   // 既定 none → bridge に
        browser: {
          allowHostControl: true,                          // ホストのブラウザを操作許可
          allowedControlUrls: ["ws://host.docker.internal:9222"],
          allowedControlHosts: ["host.docker.internal"],
          allowedControlPorts: [9222, 8080, 8009, 50021],  // CDP/LLM/2048(8009)/VOICEVOX
          cdpSourceRange: "172.21.0.1/32",                 // コンテナ縁のCDP ingressをCIDR制限
        },
      },
    },
  },
}
```

- `host.docker.internal` は Compose 側でホストゲートウェイにマップ済み（§12-④）。
  ホストサービスは `http://host.docker.internal:{8080,8009,50021}` で到達。
- `allowHostControl: true` + `allowedControlUrls` で**サンドボックスからホストの Chrome(:9222)を
  CDP 操作**できる（= Phase 0 で検証した connect_over_cdp をコンテナ→ホストで行う構成が公式サポート）。
- 注意: ブラウザサンドボックスは SSH バックエンドでは非対応（Docker ならOK）。

## §12-④ Docker イメージは公式かビルドか → **公式プリビルトあり（ghcr）。ただし Python 同梱の自前画像を推奨**

- 公式: `ghcr.io/openclaw/openclaw:latest`（タグ `main` / `latest` / `2026.x.y`）。
  セットアップは `scripts/docker/setup.sh`（画像取得→onboard→`.env`にgatewayトークン生成→Compose起動）。
  ```bash
  export OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:latest"
  ./scripts/docker/setup.sh
  ```
- Compose: Control UI を `18789` で公開。`host.docker.internal` をホストゲートウェイにマップ。
  ボリューム: `OPENCLAW_CONFIG_DIR→/home/node/.openclaw`, `OPENCLAW_WORKSPACE_DIR→.../workspace`。
  主要 env: `OPENCLAW_IMAGE` / `OPENCLAW_SANDBOX`(`1`等) / `OPENCLAW_SKIP_ONBOARDING` /
  `OPENCLAW_HOME_VOLUME`。ベース `node:24-bookworm-slim`、ユーザ `node`(uid1000)、`tini` init。
- **本デモの選択**: `exec` で Playwright(Python) を回す必要があるため、
  (a) 公式画像に Python+playwright を足した**自前画像をビルド**、または
  (b) `sandbox.docker.setupCommand` で起動時インストール（network egress + writable root 要）。
  → 再現性とデモ安定性から **(a) 自前画像**を推奨。
  なお Phase 0 で確認した通り **connect_over_cdp はブラウザ本体をDLしない**ので、
  画像には `playwright` パッケージのみでよく、Chromium はホスト側を使う（軽量）。

---

## Phase 1 の具体構成（上記から確定する形）

```
ホスト(gfx1150)
 ├ 2048 :8009            （python -m http.server, ~/2048。:8000はthree-vrmが使用）
 ├ Chrome :9222 headed   （観客に見せる窓。CDP有効）
 ├ llama-server :8080    （Qwen3, -np2, AIzunda共用）
 └ VOICEVOX :50021
        ▲ host.docker.internal:{9222,8080,8009,50021}
 Docker Compose: openclaw gateway（自前画像: 公式+Python+playwright）
   - models.providers."llama-host" → host.docker.internal:8080/v1（ダミーキー）
   - agents.defaults.model.primary = "llama-host/qwen3"
   - agents.defaults.sandbox: network=bridge, browser.allowHostControl=true,
       allowedControlUrls/Hosts/Ports に 9222/8080/8009/50021
   - skill: play2048（SKILL.md + 同梱 play2048_cdp.py、execで実行）
```

### Phase 1 残タスク（レビュー反映済み: 案B + 論点3/4 の決定）
1. `play2048_cdp.py` を **CLI サブコマンド化**（`read`/`solve`/`press`/`newgame` ＋束ねた `step`、
   保険の `play`）。各コマンドは connect_over_cdp し localStorage を都度読む（ステートレス）。
2. 自前 Dockerfile（`ghcr.io/openclaw/openclaw` + Python3 + `pip install playwright`）。
   ※完全オフライン前提のため setupCommand 起動時インストールは不可 → 画像に焼き込み（論点3）。
3. `openclaw.json`（providers."llama-host" / model.primary / sandbox）。
   ネットワークは **まず `sandbox.docker.network:"bridge"` のみ**で host 到達を試す。
   組み込み browser allowlist（allowedControlUrls等）は自前 connect_over_cdp では不要の見込み（論点2, 要実機）。
   onboard は `OPENCLAW_SKIP_ONBOARDING` でスキップし openclaw.json を事前配置（論点4, 要実機）。
4. `skills/play2048/SKILL.md`: エージェントに「`step`→`narrate` を1ターンずつ `exec` で呼び、
   `over`/`won` まで繰り返せ」と指示（`{baseDir}` でCLI参照）。
5. `scripts/docker/setup.sh` 系で Compose 起動 → Control UI(:18789) 疎通。
6. コンテナ→host:9222/8080/8009/50021 到達確認 → エージェントから1ゲーム完走（実況なしでOK）。

---

## ✅ Phase 1 実装結果（2026-06-07 実機検証済み）

成果物は `openclaw-demo/`（`Dockerfile` / `docker-compose.yml` / `.env` /
`state/openclaw.json` / `state/workspace/skills/play2048/`）。実リポジトリ `~/openclaw`(v2026.6.x) を clone して構成を確認した上で実装。

**検証できたこと**:
- 自前画像 `openclaw-2048:local`（公式 ghcr 画像 + Python3.11 + playwright1.60、Chromium本体はDLせず）ビルド成功。
- Gateway 起動成功（`agent model: llama-host/qwen3 (thinking=off)` 読込、healthz OK）。
- `play2048` スキルが **`✓ ready`** で読込（python3+linux ゲート通過、source: openclaw-workspace）。
- **コンテナ→ホスト Chrome CDP の到達を実証**: コンテナ内 Playwright が host の Chrome(:9222)へ
  connect_over_cdp し、`play` でフルゲーム完走（**1024手, 2048達成, 4.5s**）。compose の cli サービス経由でも read 成功。

**設計を確定させた実機の学び（findings 前半の仮説を更新）**:
1. **サンドボックスは使わない** → `exec` は gateway コンテナ内で直接実行（Python+playwright 同梱）。
   §12-③で検討した sandbox bridge / browser allowlist は**不要**だった。
2. **Chrome 149 は remote-debugging を 127.0.0.1 にしか bind しない**
   （`--remote-debugging-address=0.0.0.0` を無視）。→ bridge+`host.docker.internal` では CDP に届かない。
   **`network_mode: host` を採用**（CLAUDE.md §7 の「Linux なら host が最簡」に一致）。これで localhost で
   CDP/2048/llama/VOICEVOX 全てに到達でき、Chrome の Host ヘッダ(DNS-rebinding)制限も localhost で回避。
   Chrome 起動には `--remote-allow-origins=*` も付与（WebSocket 接続許可）。
3. Gateway は `gateway.mode: "local"` が無いと起動ブロック → openclaw.json に明記。
4. provider は `vllm.md` 準拠で `api: openai-completions` + `compat.thinkingFormat: "qwen-chat-template"`。

**✅ LLM 駆動のエージェントループ 実証済み（2026-06-07）**
llama-server :8080 を起動し、`docker compose run --rm openclaw-cli agent --agent main --message "..."`
でエージェントに play2048 を実行させた結果、**qwen3 がスキルを呼び `exec`→同梱Python→ホスト Chrome
CDP 経由で実際に3手着手＋ずんだもん口調で実況**（ホスト盤面が score4/max4 に変化）。案B（毎ターン
エージェント制御）が end-to-end で成立。

この検証で洗い出した統合課題と対処（task13 の実機知見）:
1. `openclaw agent` は対象セッション必須 → `--agent main`（既定エージェント）を指定。
2. **Context overflow**: OpenClaw のシステムプロンプト（多数スキル＋プラグイン）が大きく、
   実プロンプトは 16k〜数十k トークン規模。
   - `-c 8192 --parallel 2`（=4k/slot）では全く足りない。
   - `skills.allowBundled: []` で bundled スキル(約57個)をプロンプトから除外。
   - llama を `-c 65536 --parallel 1`（単一スロット64k）にして収めた。openclaw.json の
     `contextWindow` も整合（65536）。
   - 補足: KV キャッシュのプレフィックス再利用が効き、**2手目以降の prompt eval は ~150-200tok のみ**
     （毎ターン全プロンプト再評価ではない）→ テンポは実用的。
3. **host ネットワークでは `host.docker.internal` が解決できない**（extra_hosts も無い）。
   provider baseUrl を `http://localhost:8080/v1` に修正（host net なので localhost が host を指す）。
4. Qwen3 のツール呼び出しは llama.cpp 経由でも正しく `exec` を発火（text 化せず）。

**デモ向けチューニング残**（Phase 2/3）:
- 確定事項7（`--parallel 2`+）と両立させるには、プロンプトが収まる範囲で
  `-c 65536 --parallel 2`（=32k/slot）等に。実プロンプト量（16k〜）次第。要計測。
- 1ゲーム=約1000手をエージェントの毎ターン LLM で回すのは重い。短時間デモは手数を区切るか、
  テンポ重視時は保険の `play`（モノリシック）に退避（SKILL.md に明記済み）。

### 未確認 / 次に実機で確かめるべき点
- `connect_over_cdp` をコンテナ内 Playwright から `ws://host.docker.internal:9222` で張れるか
  （Chrome を `--remote-debugging-address=0.0.0.0` で起動する必要が出る可能性。start_phase0.sh は対応済み）。
- `allowedControlUrls` は OpenClaw 組み込み browser ツール用の allowlist だが、**我々は自前 Python の
  connect_over_cdp** を使う。サンドボックスの egress（network=bridge）が通れば組み込みbrowser allowlistに
  依存せず到達できるはず。実機で要検証（組み込みbrowserツールは使わず exec+python に寄せる方針）。
- onboard がオフラインで完了するか（APIキー入力をスキップ: `OPENCLAW_SKIP_ONBOARDING`）。

---

## Phase 1 仕上げ結果（2026-06-07 追記）— parallel 2 確定

上記 2.（Context overflow）の「`-c 65536 --parallel 1`」を **parallel 2 に更新**。Phase 1 完了。

### 訂正したメンタルモデル
- 当初「`contextWindow` = per-slot n_ctx に一致させる」と考えたが**誤り**。
- OpenClaw のガードは「プロンプト ≤ `contextWindow/2`」。許す**最大使用量** = `contextWindow/2 + maxTokens`
  = 24576 + 1024 = **25600 トークン**。これが llama のスロットに収まれば良い（スロット ≥ 25600）。
- → スロットを contextWindow に揃える必要はなく、**スロット 32768（= `-c 65536 --parallel 2`）で十分**。
  これで「parallel 2 は per-slot 49152 が要り総98k で非現実的」という以前の結論が覆った。

### 計測（プラグイン deny 適用後）
- 計測法: llama を別ログ `/tmp/llama-meas.log` で `-c 65536 --parallel 2` 新規起動 →
  `--session-key` 新規でエージェント1回 → task 0 の `prompt eval ... / N tokens`。
- **初回プロンプト P = 16,902 トークン**（旧 ~21k → 約20%減。`skills.allowBundled:[]` + plugins.deny 7個の効果）。
- 2手目以降は KV プレフィックス再利用で追加 ~150-730tok のみ。

### 確定構成（検証済み）
- llama: **`-c 65536 --parallel 2`**（per-slot n_ctx = 32768）。
- openclaw.json: `contextWindow: 49152`, `maxTokens: 1024`（変更なし）。
- 検証: 「最初の2手 step→narrate」エージェント実行が **exit 0**、**ピーク n_past = 19,610（truncated=0）**、
  ホスト盤面も着手で変化。**VRAM 24.8GB / 48GB**（余裕）。
- 受け入れ基準「共用 llama 上で AIzunda 会話と実況呼び出しが直列化しない（`--parallel 2`+）」を満たす。
