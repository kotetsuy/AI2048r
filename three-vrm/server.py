#!/usr/bin/env python3
"""
TalkingHead サーバー
- http://localhost:8000/zundamon.html  ← ブラウザで開く
- POST /speak  {"text": "...", "speaker_id": 3}  ← パイプラインから呼ぶ
"""
import asyncio
import base64
import io
import json
import os
import re
import signal
import uuid
import wave
import weakref


def _wav_duration_sec(wav_bytes: bytes) -> float:
    """合成 WAV の再生秒数（前後無音込み）。失敗時は 0.0。"""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            fr = wf.getframerate() or 1
            return wf.getnframes() / float(fr)
    except Exception:
        return 0.0

import aiohttp
from aiohttp import web

VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")
TTLLM_URL = os.getenv("TTLLM_URL", "http://localhost:8001")
TTLLM_TIMEOUT = float(os.getenv("TTLLM_TIMEOUT", "180"))
# 音声で「〜を案内して」等の移動・案内コマンドが来たら earth-bridge へ flyTo を送る。
EARTH_BRIDGE_URL = os.getenv("EARTH_BRIDGE_URL", "http://localhost:8002")
# flyTo 後この秒数だけ待ってから情報パネルを閉じる (背景を綺麗にする)。
FLY_DISMISS_DELAY = float(os.getenv("FLY_DISMISS_DELAY", "8"))
VRM_DIR = os.path.expanduser("~/AIassistant/vroid")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TalkingHead")
IMAGES_DIR = os.getenv("IMAGES_DIR", os.path.expanduser("~/AIzunda/images"))

clients: weakref.WeakSet = weakref.WeakSet()
# 背景ライブ配信用: bg_clients=表示ページ(zundamon.html)の購読者、
# ブリッジ(play2048_bgcast.py)が /bg_ingest に JPEG を流し、ここから各購読者へ中継する。
# earth-bridge と同様、最新フレームをキャッシュして新規購読者へ即送る。
bg_clients: weakref.WeakSet = weakref.WeakSet()
last_bg_frame: bytes | None = None

# flyTo など、リクエストの寿命を超えて走らせたいタスクの参照保持 (GC 回避)。
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

VOWEL_TO_VISEME = {
    "a": "aa", "i": "I", "u": "U", "e": "E", "o": "O",
    "N": "nn", "cl": "sil", "pau": "sil",
}

CONSONANT_TO_VISEME = {
    "p": "PP",  "b": "PP",  "m": "PP",
    "py": "PP", "by": "PP", "my": "PP",
    "f": "FF",
    "s": "SS",  "z": "SS",  "sh": "SS",
    "t": "DD",  "d": "DD",  "ts": "DD",
    "k": "kk",  "g": "kk",  "ky": "kk", "gy": "kk",
    "ch": "CH", "j": "CH",
    "n": "nn",  "ny": "nn",
    "r": "RR",  "ry": "RR",
    "h": "sil", "hy": "sil", "w": "sil", "y": "sil",
}


def mora_to_visemes(accent_phrases: list) -> tuple[list, list, list]:
    """VOICEVOXのaccentPhrasesをTalkingHeadのvisemeデータに変換する。"""
    visemes, vtimes, vdurations = [], [], []
    t = 0.0

    for phrase in accent_phrases:
        for mora in phrase.get("moras", []):
            c = mora.get("consonant")
            cl = mora.get("consonant_length") or 0.0
            v = mora.get("vowel", "pau")
            vl = mora.get("vowel_length") or 0.0

            if c and cl > 0:
                visemes.append(CONSONANT_TO_VISEME.get(c, "sil"))
                vtimes.append(int(t * 1000))
                vdurations.append(max(1, int(cl * 1000)))
                t += cl

            if v and vl > 0:
                visemes.append(VOWEL_TO_VISEME.get(v, "sil"))
                vtimes.append(int(t * 1000))
                vdurations.append(max(1, int(vl * 1000)))
                t += vl

        pause = phrase.get("pause_mora")
        if pause:
            pl = pause.get("vowel_length") or 0.0
            if pl > 0:
                visemes.append("sil")
                vtimes.append(int(t * 1000))
                vdurations.append(max(1, int(pl * 1000)))
                t += pl

    return visemes, vtimes, vdurations


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        clients.discard(ws)
    return ws


async def bg_handler(request: web.Request) -> web.WebSocketResponse:
    """背景ライブ配信の購読者(表示ページ)。送られてくる JPEG バイナリを受けて描画する。"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    bg_clients.add(ws)
    # 新規購読者には直近フレームを即送り、再接続でも背景がすぐ出るようにする。
    if last_bg_frame is not None:
        try:
            await ws.send_bytes(last_bg_frame)
        except Exception:
            pass
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        bg_clients.discard(ws)
    return ws


async def bg_ingest_handler(request: web.Request) -> web.WebSocketResponse:
    """背景フレームの供給元(ブリッジ play2048_bgcast.py)。受けた JPEG を全購読者へ中継する。"""
    global last_bg_frame
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.BINARY:
            continue
        last_bg_frame = msg.data
        dead = []
        for c in list(bg_clients):
            try:
                await c.send_bytes(msg.data)
            except Exception:
                dead.append(c)
        for c in dead:
            bg_clients.discard(c)
    return ws


async def _broadcast(message: dict) -> int:
    """接続中の全 WS クライアントに JSON を送る。"""
    payload = json.dumps(message, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
    return len(clients)


async def _synth_chunk(
    session: aiohttp.ClientSession, text: str, speaker_id: int
) -> tuple[bytes, list, list, list]:
    """1 文ぶんの WAV + visemes を合成して返す (ブロードキャストはしない)。"""
    async with session.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": speaker_id},
    ) as resp:
        if resp.status != 200:
            raise web.HTTPBadGateway(
                reason=f"audio_query failed ({resp.status}): {await resp.text()}"
            )
        query = await resp.json()

    async with session.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        json=query,
        headers={"Content-Type": "application/json"},
    ) as resp:
        if resp.status != 200:
            raise web.HTTPBadGateway(
                reason=f"synthesis failed ({resp.status}): {await resp.text()}"
            )
        wav_bytes = await resp.read()

    visemes, vtimes, vdurations = mora_to_visemes(query.get("accent_phrases", []))
    return wav_bytes, visemes, vtimes, vdurations


async def _synthesize_and_broadcast(text: str, speaker_id: int) -> dict:
    """VOICEVOX → WAV + visemes を生成し、接続中の WS クライアントに配信。"""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": speaker_id},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise web.HTTPBadGateway(
                    reason=f"audio_query failed ({resp.status}): {body}"
                )
            query = await resp.json()

        async with session.post(
            f"{VOICEVOX_URL}/synthesis",
            params={"speaker": speaker_id},
            json=query,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise web.HTTPBadGateway(
                    reason=f"synthesis failed ({resp.status}): {body}"
                )
            wav_bytes = await resp.read()

    visemes, vtimes, vdurations = mora_to_visemes(query.get("accent_phrases", []))

    # 各発話の前に turn_start を送る。表示ページはこれで字幕バッファ(botReplyBuf)を
    # リセットし、前発話の再生を止める。これが無いと /speak の text が累積し続け、
    # 字幕が伸び続ける／3行クランプで先頭だけ残り「更新が止まって見える」不具合になる。
    # （turn_start/turn_end はストリーム音声チャット専用だったが、単発 /speak にも適用する）
    await _broadcast({"type": "turn_start", "turn_id": "speak"})

    message = json.dumps({
        "type": "speak",
        "audio": base64.b64encode(wav_bytes).decode("ascii"),
        "visemes": visemes,
        "vtimes": vtimes,
        "vdurations": vdurations,
        "text": text,
    })

    dead = []
    for ws in list(clients):
        try:
            await ws.send_str(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)

    # duration_sec: 呼び出し側のテンポ同期用（喋り終わるまで次手を待たせる）。
    # 追加フィールドなので既存クライアントには後方互換。
    return {"visemes": len(visemes), "clients": len(clients),
            "duration_sec": round(_wav_duration_sec(wav_bytes), 3)}


async def speak_handler(request: web.Request) -> web.Response:
    data = await request.json()
    text: str = data.get("text", "").strip()
    speaker_id: int = data.get("speaker_id", 3)

    if not text:
        return web.json_response({"error": "no text"}, status=400)

    try:
        result = await _synthesize_and_broadcast(text, speaker_id)
    except web.HTTPBadGateway as e:
        return web.json_response({"error": e.reason}, status=502)

    return web.json_response({"ok": True, **result})


async def voice_chat_speak_handler(request: web.Request) -> web.Response:
    """音声 → ttllm (/voice_chat) → VOICEVOX 合成 → WS 配信 をワンショットで実行。"""
    reader = await request.multipart()

    audio_field = None
    speaker_id = 3
    system: str | None = None
    history: str | None = None
    temperature = 0.7
    max_tokens = 512

    async for part in reader:
        if part.name == "audio":
            audio_field = (part.filename or "audio.wav", await part.read(decode=False))
        elif part.name == "speaker_id":
            try:
                speaker_id = int((await part.text()).strip())
            except ValueError:
                pass
        elif part.name == "system":
            system = await part.text()
        elif part.name == "history":
            history = await part.text()
        elif part.name == "temperature":
            try:
                temperature = float((await part.text()).strip())
            except ValueError:
                pass
        elif part.name == "max_tokens":
            try:
                max_tokens = int((await part.text()).strip())
            except ValueError:
                pass

    if not audio_field:
        return web.json_response({"error": "audio field required"}, status=400)

    filename, audio_bytes = audio_field

    form = aiohttp.FormData()
    form.add_field("audio", audio_bytes, filename=filename,
                   content_type="application/octet-stream")
    form.add_field("temperature", str(temperature))
    form.add_field("max_tokens", str(max_tokens))
    if system is not None:
        form.add_field("system", system)
    if history is not None:
        form.add_field("history", history)

    timeout = aiohttp.ClientTimeout(total=TTLLM_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{TTLLM_URL}/voice_chat", data=form) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return web.json_response(
                        {"error": f"ttllm /voice_chat failed ({resp.status}): {body}"},
                        status=502,
                    )
                chat = await resp.json()
    except aiohttp.ClientError as e:
        return web.json_response({"error": f"ttllm unreachable: {e}"}, status=502)

    transcript = (chat.get("transcript") or "").strip()
    reply = (chat.get("reply") or "").strip()

    if not reply:
        return web.json_response({
            "ok": True,
            "transcript": transcript,
            "reply": "",
            "visemes": 0,
            "clients": len(clients),
            "note": "empty reply (no transcript or LLM returned empty)",
        })

    try:
        result = await _synthesize_and_broadcast(reply, speaker_id)
    except web.HTTPBadGateway as e:
        return web.json_response(
            {"error": e.reason, "transcript": transcript, "reply": reply},
            status=502,
        )

    return web.json_response({
        "ok": True,
        "transcript": transcript,
        "reply": reply,
        **result,
    })


_SENTENCE_END = re.compile(r"[。！？!?\n]")
_SOFT_BREAK = re.compile(r"[、,]")
_MAX_CHUNK_CHARS = 60  # 読点も句点も来ない長文の保険


def _split_sentences(buf: str, flush: bool = False) -> tuple[list[str], str]:
    """buf から完成した文を切り出す。flush=True なら残りも全部返す。"""
    out: list[str] = []
    while True:
        m = _SENTENCE_END.search(buf)
        if m:
            end = m.end()
            piece = buf[:end].strip()
            buf = buf[end:]
            if piece:
                out.append(piece)
            continue
        if len(buf) >= _MAX_CHUNK_CHARS:
            m2 = _SOFT_BREAK.search(buf, _MAX_CHUNK_CHARS // 2)
            cut = m2.end() if m2 else _MAX_CHUNK_CHARS
            piece = buf[:cut].strip()
            buf = buf[cut:]
            if piece:
                out.append(piece)
            continue
        break
    if flush and buf.strip():
        out.append(buf.strip())
        buf = ""
    return out, buf


# 移動・案内の意図を粗く拾う事前フィルタ。該当時のみ LLM に行き先を抽出させる
# (普通の雑談で無駄な LLM 呼び出し・誤 flyTo を防ぐ)。最終判断は LLM が行う。
_GUIDE_INTENT = re.compile(
    r"案内|連れ|ガイド|ツアー|行って|行き|向か|訪れ|訪ね|見せ|見たい|"
    r"飛んで|飛ぼ|移動|寄って|まで|に行|へ行"
)

_DEST_EXTRACT_SYSTEM = (
    "あなたは音声コマンドから『行き先』だけを抜き出す抽出器です。"
    "ユーザーの発話に、ある場所へ移動・案内してほしいという意図があれば、"
    "その場所の名前だけを返してください (例:『東京タワーを案内して』→『東京タワー』)。"
    "Google Earth の検索に使うので、地名・観光地・建物などの固有名詞を簡潔に。"
    "移動・案内の意図がない、または場所を特定できない場合は、NONE とだけ返してください。"
    "余計な説明・記号・引用符は一切付けないこと。"
)


async def _extract_destination(transcript: str) -> str | None:
    """transcript に移動・案内の意図があれば行き先(地名)を返す。なければ None。"""
    text = transcript.strip()
    if not text or not _GUIDE_INTENT.search(text):
        return None
    payload = {
        "text": text,
        "system": _DEST_EXTRACT_SYSTEM,
        "temperature": 0.0,
        "max_tokens": 32,
    }
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{TTLLM_URL}/chat", json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except aiohttp.ClientError:
        return None
    reply = (data.get("reply") or "").strip()
    # 1 行目だけを採り、前後の記号・引用符・句点を除去する。
    place = reply.splitlines()[0].strip().strip("　「」『』\"'。.") if reply else ""
    if not place or "NONE" in place.upper():
        return None
    return place


async def _flyto_from_transcript(transcript: str, turn_id: str) -> None:
    """発話から行き先を抽出し earth-bridge へ flyTo→(待機)→dismiss を送る。

    ナレーション生成と並行して背景で走らせる前提 (リクエストをブロックしない)。
    """
    place = await _extract_destination(transcript)
    if not place:
        return
    await _broadcast({"type": "flyto", "turn_id": turn_id, "place": place})
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{EARTH_BRIDGE_URL}/control",
                json={"cmd": "flyto", "place": place},
            ) as resp:
                if resp.status != 200:
                    return
            # 到着を待ってから情報パネルを閉じ、背景を綺麗にする。
            await asyncio.sleep(FLY_DISMISS_DELAY)
            async with session.post(
                f"{EARTH_BRIDGE_URL}/control",
                json={"cmd": "dismiss"},
            ):
                pass
    except aiohttp.ClientError:
        return


async def voice_chat_speak_stream_handler(request: web.Request) -> web.Response:
    """音声 → ttllm /voice_chat_stream → 文単位で VOICEVOX + WS ブロードキャスト。

    LLM デコードと TTS 合成を並列化することで体感遅延を縮める。
    """
    reader = await request.multipart()

    audio_field = None
    speaker_id = 3
    system: str | None = None
    history: str | None = None
    temperature = 0.7
    max_tokens = 512

    async for part in reader:
        if part.name == "audio":
            audio_field = (part.filename or "audio.wav", await part.read(decode=False))
        elif part.name == "speaker_id":
            try:
                speaker_id = int((await part.text()).strip())
            except ValueError:
                pass
        elif part.name == "system":
            system = await part.text()
        elif part.name == "history":
            history = await part.text()
        elif part.name == "temperature":
            try:
                temperature = float((await part.text()).strip())
            except ValueError:
                pass
        elif part.name == "max_tokens":
            try:
                max_tokens = int((await part.text()).strip())
            except ValueError:
                pass

    if not audio_field:
        return web.json_response({"error": "audio field required"}, status=400)

    filename, audio_bytes = audio_field

    turn_id = uuid.uuid4().hex
    transcript = ""
    reply_accum = ""
    sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
    chunks_sent = 0

    await _broadcast({"type": "turn_start", "turn_id": turn_id})

    async def tts_consumer():
        """sentence_q を順に VOICEVOX → WS へ流す (順序保証のため直列)。"""
        nonlocal chunks_sent
        async with aiohttp.ClientSession() as session:
            while True:
                sentence = await sentence_q.get()
                if sentence is None:
                    return
                try:
                    wav, visemes, vtimes, vdurations = await _synth_chunk(
                        session, sentence, speaker_id
                    )
                except web.HTTPBadGateway as e:
                    await _broadcast({"type": "error", "turn_id": turn_id, "error": e.reason})
                    continue
                await _broadcast({
                    "type": "speak",
                    "turn_id": turn_id,
                    "seq": chunks_sent,
                    "audio": base64.b64encode(wav).decode("ascii"),
                    "visemes": visemes,
                    "vtimes": vtimes,
                    "vdurations": vdurations,
                    "text": sentence,
                })
                chunks_sent += 1

    consumer_task = asyncio.create_task(tts_consumer())

    form = aiohttp.FormData()
    form.add_field("audio", audio_bytes, filename=filename,
                   content_type="application/octet-stream")
    form.add_field("temperature", str(temperature))
    form.add_field("max_tokens", str(max_tokens))
    if system is not None:
        form.add_field("system", system)
    if history is not None:
        form.add_field("history", history)

    timeout = aiohttp.ClientTimeout(total=TTLLM_TIMEOUT)
    buf = ""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{TTLLM_URL}/voice_chat_stream", data=form
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    await sentence_q.put(None)
                    await consumer_task
                    await _broadcast({"type": "turn_end", "turn_id": turn_id})
                    return web.json_response(
                        {"error": f"ttllm /voice_chat_stream failed ({resp.status}): {body}"},
                        status=502,
                    )
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="ignore").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data:
                        continue
                    try:
                        msg = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    t = msg.get("type")
                    if t == "transcript":
                        transcript = msg.get("text", "") or ""
                        await _broadcast({
                            "type": "transcript",
                            "turn_id": turn_id,
                            "text": transcript,
                        })
                        # 行き先指示なら Earth を flyTo (ナレーション生成と並行)。
                        if transcript:
                            _spawn(_flyto_from_transcript(transcript, turn_id))
                    elif t == "token":
                        buf += msg.get("text", "") or ""
                        sentences, buf = _split_sentences(buf, flush=False)
                        for s in sentences:
                            reply_accum += s
                            await sentence_q.put(s)
                    elif t == "error":
                        await _broadcast({
                            "type": "error",
                            "turn_id": turn_id,
                            "error": msg.get("error", ""),
                        })
                    elif t == "done":
                        final_reply = msg.get("reply", "")
                        if final_reply:
                            reply_accum = final_reply
                        break
    except aiohttp.ClientError as e:
        await sentence_q.put(None)
        await consumer_task
        await _broadcast({"type": "turn_end", "turn_id": turn_id})
        return web.json_response({"error": f"ttllm unreachable: {e}"}, status=502)

    tail, _ = _split_sentences(buf, flush=True)
    for s in tail:
        if not reply_accum.endswith(s):
            reply_accum += s
        await sentence_q.put(s)

    await sentence_q.put(None)
    await consumer_task

    await _broadcast({
        "type": "turn_end",
        "turn_id": turn_id,
        "chunks": chunks_sent,
    })

    return web.json_response({
        "ok": True,
        "transcript": transcript,
        "reply": reply_accum,
        "chunks": chunks_sent,
        "turn_id": turn_id,
    })


async def vrm_handler(request: web.Request) -> web.Response:
    filename = os.path.basename(request.match_info["filename"])
    filepath = os.path.join(VRM_DIR, filename)
    if not os.path.isfile(filepath):
        raise web.HTTPNotFound()
    return web.FileResponse(filepath)


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


async def images_list_handler(request: web.Request) -> web.Response:
    if not os.path.isdir(IMAGES_DIR):
        return web.json_response({"images": []})
    files = sorted(
        f for f in os.listdir(IMAGES_DIR)
        if f.lower().endswith(_IMAGE_EXTS)
    )
    return web.json_response({
        "images": [f"/images/{f}" for f in files],
    })


async def status_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "clients": len(clients),
        "bg_clients": len(bg_clients),
        "voicevox": VOICEVOX_URL,
        "vrm_dir": VRM_DIR,
    })


DEMO_PIDFILE = os.environ.get("DEMO_PIDFILE", "/tmp/demo_loop.pid")


def _proc_is_demo_loop(pid: int) -> bool:
    """PID の実体が demo_loop.sh かを /proc/<pid>/cmdline で確認（PID 再利用・古い
    pidfile による誤送信を防ぐ）。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return "demo_loop.sh" in cmd


async def _signal_loop() -> bool:
    """demo_loop.sh に SIGINT を送る（PID ファイル経由）。送れたら True。
    PID ファイルを読み、その PID が実際に demo_loop.sh の場合のみ送る（pkill -f の
    誤マッチ＝文字列を含む無関係プロセスへの誤送信を避ける）。"""
    try:
        with open(DEMO_PIDFILE) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False  # ループ未実行
    if not _proc_is_demo_loop(pid):
        return False  # 古い pidfile / PID 再利用
    try:
        os.kill(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _stop_agent_sessions() -> int:
    """進行中の OpenClaw エージェントセッション（compose run の cli-run コンテナ）を
    止める。停止したコンテナ数を返す。これで「いま喋っている手」も数秒で打ち切れる
    （即時停止）。gateway コンテナ等は名前フィルタで除外される。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-q", "--filter", "name=openclaw-cli-run",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except FileNotFoundError:
        return 0
    ids = out.decode().split()
    if not ids:
        return 0
    proc2 = await asyncio.create_subprocess_exec(
        "docker", "stop", "-t", "3", *ids,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc2.wait()
    return len(ids)


async def stop_demo_handler(request: web.Request) -> web.Response:
    """連続稼働ループ demo_loop.sh を即時停止する（アバター画面の停止ボタン用）。
    (1) ループに SIGINT を送り次セッションの開始を止め、(2) 進行中のエージェント
    セッション(cli-run コンテナ)も止めて、いま喋っている手も数秒で打ち切る。
    サービス一式は止めない（それは stop_all.sh の役目）。"""
    signaled = await _signal_loop()
    stopped = await _stop_agent_sessions()
    return web.json_response(
        {"ok": True, "signaled": signaled, "stopped_sessions": stopped}
    )


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/bg", bg_handler)               # 表示ページ: 背景フレーム購読
    app.router.add_get("/bg_ingest", bg_ingest_handler) # ブリッジ: 背景フレーム投入
    app.router.add_post("/speak", speak_handler)
    app.router.add_post("/voice_chat_speak", voice_chat_speak_handler)
    app.router.add_post("/voice_chat_speak_stream", voice_chat_speak_stream_handler)
    app.router.add_get("/vrm/{filename}", vrm_handler)
    app.router.add_get("/images_list", images_list_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_post("/stop_demo", stop_demo_handler)  # 停止ボタン: demo_loop.sh に Ctrl+C 相当
    if os.path.isdir(IMAGES_DIR):
        app.router.add_static("/images", IMAGES_DIR)
    app.router.add_static("/", STATIC_DIR, show_index=True)
    return app


if __name__ == "__main__":
    app = create_app()
    print("=" * 50)
    print("TalkingHead server: http://localhost:8000")
    print("Avatar page:        http://localhost:8000/zundamon.html")
    print("Speak endpoint:     POST http://localhost:8000/speak")
    print('  body: {"text": "ずんだもんなのだ", "speaker_id": 3}')
    print("Voice chat endpoint: POST http://localhost:8000/voice_chat_speak")
    print("  multipart: audio=<file> [speaker_id=3] [system=...] [history=...]")
    print(f"  ttllm: {TTLLM_URL}  (WhisperX + llama.cpp)")
    print(f"  voicevox: {VOICEVOX_URL}")
    print("=" * 50)
    web.run_app(app, host="0.0.0.0", port=8000)
