#!/usr/bin/env python3
"""Boogu-Image 本地生图服务（OpenAI 兼容 /v1/images/generations）

依赖：
  - mlx / mlx-vlm / fastapi / uvicorn / pillow
  - boogu-image-mlx 管线（https://github.com/xocialize/boogu-image-mlx）
  - 模型：
      BOOGU_MODEL  mlx-community/Boogu-Image-0.1-Turbo-8bit
      BOOGU_QWEN   Qwen3-VL-8B-Instruct-4bit（prompt 理解辅助）

环境变量（均可覆盖，默认在 ~/.boogu 下）：
  BOOGU_PKG    boogu-image-mlx 目录
  BOOGU_MODEL  主模型目录
  BOOGU_QWEN   辅助 VLM 目录
  BOOGU_PORT   服务端口（默认 8081）
"""
import base64
import gc
import io
import os
import random
import sys
import threading
import time

BOOGU_PKG = os.environ.get("BOOGU_PKG", os.path.expanduser("~/.boogu/boogu-image-mlx"))
BOOGU_MODEL = os.environ.get("BOOGU_MODEL", os.path.expanduser("~/.boogu/models/Boogu-Image-0.1-Turbo-8bit"))
BOOGU_QWEN = os.environ.get("BOOGU_QWEN", os.path.expanduser("~/.boogu/models/Qwen3-VL-8B-Instruct-4bit"))
BOOGU_PORT = int(os.environ.get("BOOGU_PORT", "8081"))
IDLE_UNLOAD_SECONDS = 120

sys.path.insert(0, BOOGU_PKG)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image
from boogu_image_mlx.pipeline_mlx import BooguImagePipeline

app = FastAPI()
pipe = None
_last_req = 0.0


def get_pipe():
    global pipe, _last_req
    if pipe is None:
        t0 = time.time()
        pipe = BooguImagePipeline.from_pretrained(BOOGU_MODEL, BOOGU_QWEN)
        print(f"[boogu] 模型加载完成: {time.time() - t0:.1f}s", flush=True)
    _last_req = time.time()
    return pipe


def _idle_unloader():
    """空闲超过 IDLE_UNLOAD_SECONDS 后卸载模型，释放内存"""
    global pipe
    while True:
        time.sleep(30)
        if pipe is not None and time.time() - _last_req > IDLE_UNLOAD_SECONDS:
            pipe = None
            import mlx.core as mx
            mx.clear_cache()
            gc.collect()
            print(f"[boogu] 空闲 {IDLE_UNLOAD_SECONDS}s，已卸载模型释放内存", flush=True)


threading.Thread(target=_idle_unloader, daemon=True).start()


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "boogu-image", "object": "model", "owned_by": "local"}]}


@app.post("/v1/images/generations")
async def generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    negative = body.get("negative", "")
    size = body.get("size", "1024x1024")
    n = min(int(body.get("n", 1)), 4)
    steps = int(body.get("steps", 4))
    guidance = float(body.get("guidance", 1.0))
    seed = body.get("seed")
    if not prompt:
        return JSONResponse(status_code=400, content={"error": {"message": "prompt is required"}})
    try:
        w, h = [int(x) for x in str(size).lower().split("x")]
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": f"bad size: {size}"}})
    p = get_pipe()
    out = []
    for i in range(n):
        s = (seed + i) if seed is not None else None
        seed_val = s if s is not None else random.randint(0, 2 ** 31 - 1)  # 未传 seed 时随机，避免每次同一张图
        img = p.generate(prompt, negative=negative, height=h, width=w, steps=steps,
                         guidance=guidance, seed=seed_val)
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, format="PNG")
        out.append({"b64_json": base64.b64encode(buf.getvalue()).decode()})
    return {"created": int(time.time()), "data": out}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=BOOGU_PORT, log_level="warning")
