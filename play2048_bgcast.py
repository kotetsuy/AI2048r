#!/usr/bin/env python3
"""
play2048_bgcast.py — 2048 の画面を「背景ライブ」として配信するブリッジ。

EarthTourGuide の earth-controller と同じ方式:
  Playwright(async) + CDP Page.startScreencast で JPEG フレームを取得し、
  aiohttp の WS クライアントで three-vrm の /bg_ingest へ send_bytes する。
  three-vrm が /bg 購読者(zundamon.html)へ中継し、scene.background に描画される。
  （earth-controller は Chrome を launch するが、本スクリプトは既存の 2048 Chrome に
    connect_over_cdp して、その 2048 ページを screencast する点だけ異なる。
    生 CDP を自前 WS で扱わず Playwright 経由にするのが肝＝EarthTourGuide と同じ。）

  2048 Chrome(:9222) ──CDP screencast──► bgcast ──ws /bg_ingest──► three-vrm ──/bg──► 表示ページ背景

env:
  PLAY2048_CDP_URL   (既定 http://localhost:9222)         2048 Chrome の CDP
  PLAY2048_GAME_URL  (既定 http://localhost:8009)          配信する 2048 ページ URL
  PLAY2048_BGCAST_WS (既定 ws://localhost:8000/bg_ingest)  three-vrm の取り込み口
  PLAY2048_BGCAST_QUALITY/MAXW/MAXH/FPS  画質・解像度・送出fps
"""
import asyncio
import base64
import os

import aiohttp
from playwright.async_api import async_playwright

CDP_URL = os.getenv("PLAY2048_CDP_URL", "http://localhost:9222")
GAME_URL = os.getenv("PLAY2048_GAME_URL", "http://localhost:8009")
BGCAST_WS = os.getenv("PLAY2048_BGCAST_WS", "ws://localhost:8000/bg_ingest")
QUALITY = int(os.getenv("PLAY2048_BGCAST_QUALITY", "70"))
MAX_W = int(os.getenv("PLAY2048_BGCAST_MAXW", "720"))
MAX_H = int(os.getenv("PLAY2048_BGCAST_MAXH", "900"))
FPS = float(os.getenv("PLAY2048_BGCAST_FPS", "15"))
# 背景に映す 2048 ページのズーム。"fit"=ウィンドウに収まるよう自動縮小、数値=固定倍率。
ZOOM = os.getenv("PLAY2048_BGCAST_ZOOM", "fit")


async def apply_zoom(page, cdp):
    """2048 ページを縮小してウィンドウ（＝キャプチャ範囲）に収める。下が切れる問題対策。
    do_newgame は page.reload() するので、reload 後も効くよう addScriptToEvaluateOnNewDocument で
    再適用し、起動時にも即適用する。"""
    if ZOOM == "fit":
        # 盤面(.game-container)の下端がビューポートに収まる倍率を計算（フッター説明文は
        # フォールド外でよい＝盤面を大きく見せる）。盤面要素が無ければページ全高にフォールバック。
        # 注意: 直前の zoom が残っていると座標が歪むので、計測前に zoom=1 へ戻す。
        z = await page.evaluate("""() => {
            document.documentElement.style.zoom = 1;
            void document.body.offsetHeight;  // reflow を強制してから計測
            const gc = document.querySelector('.game-container');
            const target = gc
                ? (gc.getBoundingClientRect().bottom + window.scrollY)
                : Math.max(document.body.scrollHeight,
                           document.documentElement.scrollHeight, 1);
            const sw = Math.max(document.body.scrollWidth,
                                document.documentElement.scrollWidth, 1);
            return Math.min(1, window.innerHeight / target, window.innerWidth / sw);
        }""")
        z = max(0.3, round(float(z) * 0.97, 3))  # 少し余白
    else:
        z = float(ZOOM)
    # reload 後にも効くよう、毎ドキュメントで zoom を当てるスクリプトを仕込む。
    src = (
        "(function(){var z='%s';"
        "function a(){try{document.documentElement.style.zoom=z;}catch(e){}}"
        "a();document.addEventListener('DOMContentLoaded',a);"
        "window.addEventListener('load',a);})();" % z
    )
    try:
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": src})
    except Exception:
        pass
    try:
        await page.evaluate("(z)=>{document.documentElement.style.zoom=z;}", z)
    except Exception:
        pass
    return z


def _find_game_page(browser):
    """connect 済み browser から 2048 ページを探す。無ければ最初のページ。"""
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if GAME_URL in pg.url:
                return ctx, pg
    ctx = browser.contexts[0]
    return ctx, ctx.pages[0]


async def main():
    latest = {"jpeg": None, "n": 0}
    stop = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        browser.on("disconnected", lambda: stop.set())
        ctx, page = _find_game_page(browser)
        print(f"[bgcast] 2048 page: {page.url}", flush=True)

        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Page.enable")

        z = await apply_zoom(page, cdp)
        print(f"[bgcast] 2048 zoom={z}（ウィンドウに収める）", flush=True)

        async def on_frame(params):
            # ack を最優先（怠ると screencast が止まる）— earth-controller と同じ。
            try:
                await cdp.send("Page.screencastFrameAck",
                               {"sessionId": params["sessionId"]})
            except Exception:
                pass
            latest["jpeg"] = base64.b64decode(params["data"])
            latest["n"] += 1

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": QUALITY,
            "maxWidth": MAX_W, "maxHeight": MAX_H, "everyNthFrame": 1})
        print(f"[bgcast] screencast 開始 (q{QUALITY} {MAX_W}x{MAX_H}) → {BGCAST_WS}",
              flush=True)

        # 取り込み口(/bg_ingest)へ最新フレームを送る。新フレームのみ送る（無駄を抑制）。
        # 接続はベストエフォート＋自動再接続（three-vrm が落ちても screencast は回し続ける）。
        async def sender():
            last_n = -1
            interval = 1.0 / max(1.0, FPS)
            while not stop.is_set():
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.ws_connect(BGCAST_WS, max_msg_size=0) as ws:
                            print("[bgcast] /bg_ingest connected", flush=True)
                            while not stop.is_set():
                                if latest["jpeg"] is not None and latest["n"] != last_n:
                                    await ws.send_bytes(latest["jpeg"])
                                    last_n = latest["n"]
                                await asyncio.sleep(interval)
                except Exception as e:
                    print(f"[bgcast] /bg_ingest 切断 ({e}); 2秒後に再接続", flush=True)
                    await asyncio.sleep(2)

        sender_task = asyncio.create_task(sender())
        try:
            await stop.wait()
        finally:
            sender_task.cancel()
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:
                pass
    print("[bgcast] 終了", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
