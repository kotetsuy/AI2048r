# 2048 × OpenClaw Offline Commentary Demo

A **fully offline** trade-show demo. An AI plays a local 2048 game automatically while a VRM
avatar named "Koteko" gives live commentary. The goal is to stop people in their tracks with the
picture of "an AI looking at the screen, thinking, and talking."

- Moves are chosen by **expectimax (pure Python, CPU)** — reaches 2048 reliably.
- The board is read from **localStorage `gameState`** (not image recognition — zero error).
- The control loop is centered on an **OpenClaw (Docker) agent**: Qwen3 invokes the skill every turn.
- Commentary is synced through **VOICEVOX** (Zundamon voice) → **three-vrm** (VRM avatar).
- The 2048 board is streamed live as the background, with the avatar speaking in front of it — a **single-screen picture**.

![Architecture](images/architecture-en.svg)

> 日本語版は [`READMEJ.md`](READMEJ.md) / [`TECHNICALJ.md`](TECHNICALJ.md) を参照。
> Internal design and implementation details: [`TECHNICAL.md`](TECHNICAL.md).

---

## 1. Environment

| Item | Value |
|---|---|
| Machine | NucBox EVO X2 / Ryzen AI MAX+ 395 / gfx1150 / 48GB unified memory |
| OS | Ubuntu 24.04 |
| GPU | ROCm 7.2.x (`HSA_OVERRIDE_GFX_VERSION=11.5.0`) |
| Display/audio | xrdp + GNOME Remote Desktop, PipeWire → xrdp-sink |

Heavy inference (LLM/VLM) runs on gfx1150 (ROCm); OpenClaw itself, expectimax, and browser control run on CPU.

---

## 2. Prerequisites (assets provided outside this repo)

This repo contains the orchestration set (scripts, OpenClaw config, vendored three-vrm), but the
**large assets are supplied separately** (they are `.gitignore`d / not redistributed for license reasons).

| Required | Default path | How to obtain |
|---|---|---|
| 2048 game itself | `~/2048` | `git clone https://github.com/gabrielecirulli/2048` |
| llama.cpp (ROCm build) | `~/llama.cpp/build/bin/llama-server` | build with ROCm for gfx1150 (`HSA_OVERRIDE_GFX_VERSION=11.5.0`) |
| Qwen3 model | `~/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` | place the GGUF |
| VRM avatar | `~/AIassistant/vroid/koteko.vrm` | place the VRM file |
| VOICEVOX | Docker image | `start_all.sh` pulls/starts it automatically |

Required commands: `docker` (+ compose v2), `tmux`, `curl`, `google-chrome`, `python3`, `xrandr`.

---

## 3. Setup (git clone → run)

```bash
# 1) Clone
cd ~
git clone <this repository> AI2048
cd AI2048

# 2) Get the 2048 game (the offline serving source)
git clone https://github.com/gabrielecirulli/2048 ~/2048

# 3) Create the venv for the background cast (bgcast): playwright + aiohttp. No Chromium binary needed.
python3 -m venv .venv
./.venv/bin/pip install playwright aiohttp
#   Note: connect_over_cdp uses the host Chrome, so `playwright install` is not needed.

# 4) Prepare the Compose .env (contains paths and a token; .env is NOT in the repo)
cd openclaw-demo
STATE="$(pwd)/state"
{ echo "OPENCLAW_IMAGE=openclaw-2048:local";
  echo "OPENCLAW_CONFIG_DIR=${STATE}";
  echo "OPENCLAW_WORKSPACE_DIR=${STATE}/workspace";
  echo "OPENCLAW_GATEWAY_PORT=18789";
  echo "OPENCLAW_GATEWAY_BIND=lan";
  echo "OPENCLAW_TZ=Asia/Tokyo";
  echo "OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)"; } > .env

# 5) Build the OpenClaw container image (official image + Python + playwright)
docker compose build           # → openclaw-2048:local
cd ..
```

`openclaw-demo/.env` contains `OPENCLAW_GATEWAY_TOKEN` (a secret) and is therefore **`.gitignore`d**;
it is not part of the repo. A template lives in [`openclaw-demo/.env.example`](openclaw-demo/.env.example).
To prepare it by hand, `cp .env.example .env` and edit the paths (absolute paths in this repo) and the token.
The one-liner in step 4 generates `.env` automatically from the current directory.

---

## 4. Start (one command)

```bash
cd ~/AI2048
./start_all.sh
```

`start_all.sh` brings up the following in order (skipped if already up — idempotent):

1. 2048 static server `:8009` (tmux)
2. VOICEVOX (docker) `:50021`
3. llama-server `:8080` (`-c 65536 --parallel 2`, shared with AIzunda/EarthTourGuide)
4. three-vrm `:8000` (VRM display + `/speak` + background relay)
5. 2048 display Chrome (CDP `:9222`, headed)
6. OpenClaw gateway (docker) `:18789`
7. VRM fullscreen display + background cast (`start_phase2_display.sh`)

Host-side processes run in the tmux session `ai2048`. Logs: `tmux attach -t ai2048`;
Chrome/bgcast logs in `/tmp/chrome-*.log` and `/tmp/bgcast.log`.

### Smoke test (commentary for one game)

```bash
cd openclaw-demo
docker compose run --rm -T openclaw-cli \
  agent --agent main --session-key demo$(date +%s) \
  --message "Start a new game with the play2048 skill, then narrate with step then narrate, and report."
```

OK if it exits 0, the board on the host screen changes, and the avatar speaks.

---

## 5. Continuous run (the actual demo)

```bash
cd ~/AI2048
./demo_loop.sh
```

The outer loop calls the OpenClaw agent with a fresh `--session-key` each time, repeating
"a few moves with `steps` → `narrate` (thinned)" → end-of-game flourish → `newgame`.

![Control flow](images/control-flow-en.svg)

Tuning env vars:

| env | Default | Meaning |
|---|---|---|
| `DEMO_MOVES` | 8 | moves per session |
| `DEMO_GAP` | 2 | wait between sessions (sec) |
| `DEMO_FRESH` | 1 | newgame at start (0 = continue) |

**Stopping:**
- The **"⏹ Stop demo" button** on the avatar screen (immediate stop; also `docker stop`s the in-flight session).
- `Ctrl-C` in the terminal (stops after the current session completes).
- The start PID is recorded in `/tmp/demo_loop.pid`.

---

## 6. Stop

```bash
./stop_all.sh                 # stop the whole demo
./stop_all.sh --keep-shared   # keep the shared llama / VOICEVOX (recommended)
```

`llama-server` and `VOICEVOX` are shared with EarthTourGuide / AIzunda, so use `--keep-shared`
in a shared environment.

---

## 7. Ports

| Service | Port | Purpose |
|---|---|---|
| 2048 static server | 8009 | game serving |
| three-vrm | 8000 | VRM display / `/speak` / background relay |
| llama-server | 8080 | Qwen3 (commentary text generation, shared) |
| Chrome CDP | 9222 | bot control / background screencast source |
| OpenClaw gateway | 18789 | agent control plane |
| VOICEVOX | 50021 | speech synthesis |

---

## 8. Troubleshooting

- **Avatar does not speak**: `clients` is 0 in `curl localhost:8000/status` → the display Chrome is not
  connected to `/ws`. Re-run `start_phase2_display.sh`. Check `/tmp/chrome-vrm.log`.
- **2048 does not appear in the background**: no `/bg_ingest connected` in `/tmp/bgcast.log` → bgcast not
  started. Check that `.venv` has playwright+aiohttp. You can disable it with `NO_BGCAST=1`.
- **Agent context overflow**: start llama with `-c 65536 --parallel 2` (per-slot 32768 is required).
- **Cannot connect to CDP**: Chrome must be started headed with the `/tmp/chrome-cdp-2048` profile.
  Remove `/tmp/chrome-cdp-2048/SingletonLock` before starting.
- **`Cannot continue from message role: assistant`**: reusing session `main` → pass a unique `--session-key` each time.

For deeper design notes and known pitfalls, see [`TECHNICAL.md`](TECHNICAL.md).

---

## 9. Status

| Phase | State |
|---|---|
| Phase 0 (CDP connection feasibility) | ✅ done |
| Phase 1 (OpenClaw containerization + skill + agent-driven loop) | ✅ done (confirmed with `--parallel 2` + small context) |
| Phase 2 (real audio commentary: narrate→VOICEVOX→three-vrm) | ✅ done (verified up to VRM display + tempo sync) |
| Phase 3 (polish) | ✅ mostly done (background / start-stop scripts / continuous run / win-lose flourish / stop button / subtitles / facing-front) |

A recent 5-minute demo test ran 4 sessions with 0 failures, confirming auto-restart, Koteko-style
commentary, and tempo sync; VRAM 24.93→25.06GB (no leak). **The only remaining item is a long
(1–2 hour) endurance run.**
