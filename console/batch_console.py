#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ComfyUI 批量生成控制台（Mac 本地运行）

批量填写提示词 -> 构建 API 图 -> 提交远程 ComfyUI -> 监控/记录/下载

启动：
    python3 batch_console.py [端口]      # 默认 8890
浏览器打开：
    http://127.0.0.1:8890
"""

import base64
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)  # 项目根（config.json 所在目录）
STATE_FILE = os.path.join(BASE_DIR, "batch_state.json")
DB_FILE = os.path.join(BASE_DIR, "console.db")
RECORDS_CSV = os.path.join(BASE_DIR, "batch_records.csv")
RECORDS_MD = os.path.join(BASE_DIR, "batch_records.md")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
RULES_DIR = os.path.join(BASE_DIR, "rules")


# ---------- 配置加载（config.json，路径相对项目根，禁止绝对路径） ----------

_CONFIG_DEFAULTS = {
    "comfyui": {"server": "http://127.0.0.1:8188", "workflow_dir": "workflows"},
    "storage": {"output_dir": "comfyui_backup/outputs", "asset_dirs": ["素材", "出镜素材"]},
    "llm": {
        "provider": "local",
        "provider_type": "openai",
        "local": {"url": "http://127.0.0.1:1234", "model": "qwen3.6-27b-abliterated-mlx", "token": ""},
        "cloud": {"enabled": False, "base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o-mini"},
    },
    "image_gen": {
        "provider": "local",
        "provider_type": "openai",
        "local": {"url": "http://127.0.0.1:8081"},
        "cloud": {"enabled": False, "base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-image-1"},
        "comfyui": {
            "server": "http://127.0.0.1:8188",
            "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
            "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "vae": "minimax_h3_video_vae_fp16.safetensors",
            "size": "768x1024",
            "steps": 20,
            "target_mode": "micro_video_5_frames",
            "reference_strength": 0.75,
            "t2i_reference_strength": 0.2,
            "story_mode": "refedit",
            "timeout": 900,
        },
    },
    "vision": {"base_url": "http://127.0.0.1:8001/v1", "api_key": "", "model": "qwen-vl-max"},
    "models": {
        "r2v": {
            "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
            "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        }
    },
    "console": {"port": 8890, "max_ref_images": 8},
}


def _deep_merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _config_path():
    env = os.environ.get("BATCH_CONSOLE_CONFIG")
    if env:
        return env
    return os.path.join(PROJECT_ROOT, "config.json")


def _abs_path(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(PROJECT_ROOT, p))


def load_config():
    """读取项目根 config.json（不存在则用默认值），支持环境变量覆盖。"""
    import copy
    cfg = copy.deepcopy(_CONFIG_DEFAULTS)
    p = _config_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                _deep_merge(cfg, json.load(f))
        except Exception as e:
            print(f"[config] 读取 {p} 失败：{e}，使用默认配置", flush=True)
    # 环境变量覆盖
    if os.environ.get("COMFYUI_SERVER"):
        cfg["comfyui"]["server"] = os.environ["COMFYUI_SERVER"]
    if os.environ.get("LLM_CLOUD_API_KEY"):
        cfg["llm"]["cloud"]["api_key"] = os.environ["LLM_CLOUD_API_KEY"]
    if os.environ.get("IMAGE_CLOUD_API_KEY"):
        cfg["image_gen"]["cloud"]["api_key"] = os.environ["IMAGE_CLOUD_API_KEY"]
    return cfg


_CONFIG = load_config()

# 对外常量（兼容旧引用，全部来自配置；路径已解析为绝对路径，内部使用）
DEFAULT_SERVER = _CONFIG["comfyui"]["server"]
DEFAULT_WORKFLOW_DIR = _abs_path(_CONFIG["comfyui"]["workflow_dir"])
OUTPUTS_DIR = _abs_path(_CONFIG["storage"]["output_dir"])
IMAGE_DIRS = [_abs_path(d) for d in _CONFIG["storage"]["asset_dirs"]]
LMSTUDIO_URL = _CONFIG["llm"]["local"]["url"]
LMSTUDIO_MODEL = _CONFIG["llm"]["local"]["model"]
BOOGU_URL = _CONFIG["image_gen"]["local"]["url"]

# 项目进度状态机（0-6）：仅创建 → 剧本 → 改写 → 提示词 → 资产 → 提交 → 出片
PROGRESS_LABELS = [
    "仅创建", "剧本已生成", "剧本已改写", "提示词已生成",
    "资产已生成", "已提交任务", "已出片",
]

lock = threading.Lock()

# 异步扩写任务（逐段生成，前端轮询进度）
EXPAND_JOBS = {}
EXPAND_JOBS_LOCK = threading.Lock()
ASSEMBLE_JOBS = {}
ASSEMBLE_JOBS_LOCK = threading.Lock()
ASSET_JOBS = {}
ASSET_JOBS_LOCK = threading.Lock()


def estimate_timeout(task):
    """按 时长/分辨率 估算任务超时上限（秒），超时视为卡死。"""
    dur = float(task.get("duration") or 10)
    mp = float(task.get("mp") or 1.0)
    # 实测：768p 每 10 秒约 12 分钟（每秒 72 秒生成）；480p 每 10 秒约 2.5 分钟（每秒 15 秒）
    per_sec = 72 if mp >= 1.0 else 15
    est = 1500 + dur * per_sec  # 25 分钟加载余量 + 生成耗时
    return int(est * 1.5)  # 再留 50% 余量


# ---------- HTTP 工具 ----------

def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_get(server, path, timeout=15):
    req = urllib.request.Request(server + path, headers={"User-Agent": "batch-console"})
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(server, path, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        server + path, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "batch-console"},
    )
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def upload_image(server, local_path, filename):
    """上传参考图到服务器 input 目录（multipart，标准库手写）。"""
    with open(local_path, "rb") as f:
        raw = f.read()
    boundary = "----CodexBatch" + uuid.uuid4().hex
    parts = []

    def add_file(name, fn, ctype, content):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{fn}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8")
        )
        parts.append(content)
        parts.append(b"\r\n")

    def add_field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    add_file("image", filename, "image/png", raw)
    add_field("type", "input")
    add_field("overwrite", "true")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        server + "/api/upload/image", b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "batch-console"},
    )
    with _opener().open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- 状态持久化 ----------

def _db_connect():
    return sqlite3.connect(DB_FILE, timeout=10)


def _ensure_db():
    conn = _db_connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def load_state():
    """SQLite 键值存储。首次运行时自动把旧 batch_state.json 迁移进来。"""
    _ensure_db()
    try:
        conn = _db_connect()
        try:
            rows = conn.execute("SELECT key, value FROM state").fetchall()
        finally:
            conn.close()
        data = {k: json.loads(v) for k, v in rows}
    except Exception:
        data = {}
    if not data and os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            if isinstance(legacy, dict) and legacy:
                save_state(legacy)
                os.replace(STATE_FILE, STATE_FILE + ".bak")  # 保留备份
                data = legacy
        except Exception:
            pass
    base = {"server": DEFAULT_SERVER, "auto_download": True, "tasks": []}
    for k, v in base.items():
        data.setdefault(k, v)
    return data


def save_state(state):
    _ensure_db()
    conn = _db_connect()
    try:
        with conn:
            for k, v in state.items():
                if v is None:
                    conn.execute("DELETE FROM state WHERE key = ?", (k,))
                else:
                    conn.execute(
                        "INSERT INTO state(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (k, json.dumps(v, ensure_ascii=False)),
                    )
    finally:
        conn.close()


def find_image(filename):
    for d in IMAGE_DIRS:
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            return p
    return None


def list_images():
    found = []
    for d in IMAGE_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                found.append(fn)
    return found


def list_media():
    """列出素材目录里的视频/音频文件（参考视频用）。排除合成成片（合成_*），避免误当参考。"""
    found = []
    for d in IMAGE_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.startswith("合成_"):
                continue
            if fn.lower().endswith((".mp4", ".webm", ".mov", ".mkv", ".wav", ".mp3")):
                found.append(fn)
    return found


def scan_assets():
    """按命名规律扫描素材目录，重建资产映射（角色/场景/分镜 → 最新图片）。"""
    result = {"role": {}, "scene": {}, "story": {}}
    for d in IMAGE_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            stem = os.path.splitext(fn)[0]
            m = re.match(r"^锚点_(.+?)_[a-z0-9]+$", stem)
            if m:
                result["role"][m.group(1)] = fn
                continue
            m = re.match(r"^场景_(.+?)_[a-z0-9]+$", stem)
            if m:
                result["scene"][m.group(1)] = fn
                continue
            m = re.match(r"^分镜_s(\d+)_[a-z0-9]+$", stem)
            if m:
                result["story"][int(m.group(1)) - 1] = fn
    return result


def detect_lead_noise_sec(video_path):
    """检测 H3 视频开头的"起始音节"（约 0.02-0.15s 的短促人声音节），
    返回应裁剪的秒数；无前置音节返回 0。

    判定模式：前 0.6s 内出现高能量段（>1500）→ 之后连续 0.2s 低能量（<800）
    → 再之后又有高能量（真语音）。取低能量间隙起点为裁剪点。
    """
    try:
        tmp = tempfile.mkdtemp(prefix="lead_")
        wav = os.path.join(tmp, "lead.wav")
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "quiet", "-i", video_path, "-t", "1",
             "-vn", "-ac", "1", "-ar", "16000", wav],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.isfile(wav):
            return 0.0
        import wave as _wave
        import struct as _struct
        w = _wave.open(wav, "rb")
        frames = w.readframes(w.getnframes())
        n = w.getnframes()
        sr = w.getframerate()
        w.close()
        if n < sr:  # 不足 1 秒音频不处理
            return 0.0
        samples = _struct.unpack(f"<{n}h", frames)
        win = max(int(sr * 0.02), 1)
        rms = []
        for i in range(0, n - win, win):
            chunk = samples[i:i + win]
            rms.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)))
        win_s = win / sr
        # 第一个高能量段（前置音节起点），必须在前 0.6s 内
        hi = [i for i, r in enumerate(rms) if r > 1500 and i * win_s < 0.6]
        if not hi:
            return 0.0
        start = hi[0]
        # 高能量段必须短（<0.3s），否则是正常台词开头
        hi_end = start
        while hi_end < len(rms) and rms[hi_end] > 800:
            hi_end += 1
        if (hi_end - start) * win_s > 0.3:
            return 0.0
        # 高能量段后必须有连续 10 窗口（0.2s）低能量 <800（前置音节后的间隙）
        for i in range(hi_end, len(rms) - 10):
            if all(r < 800 for r in rms[i:i + 10]):
                cut = i * win_s
                return round(cut, 3) if 0.02 < cut < 0.8 else 0.0
        return 0.0
    except Exception:
        return 0.0
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def assemble_project_video(project_name="", selection=None, seg_range=None, progress_cb=None):
    """按项目剧本顺序合成完整视频。

    规则：取项目 prompt_tasks 的段顺序（可按 seg_range 截取起止段，1 起）；每段默认取
    "同名任务中最新提交且已下载"的一版；selection（{段名: 视频文件名}）可手动指定。
    单段转码失败跳过不中断；进度通过 progress_cb(done, total) 回调。
    返回 (filename, 绝对路径, skipped列表)。
    """
    st = load_state()
    proj = st.get("project") or {}
    pt = proj.get("prompt_tasks") or []
    all_tasks = st.get("tasks", [])
    src_meta = []
    seg_label = ""
    if pt:
        missing = []
        selection = selection or {}
        total = len(pt)
        start, end = seg_range or (0, total)
        seg_label = f"_{start + 1}-{end}"
        if not (0 <= start < end <= total):
            raise RuntimeError(f"段范围无效：{start + 1}-{end}（共 {total} 段）")
        for seg in pt[start:end]:
            sname = str(seg.get("name") or "")
            if not sname:
                continue
            cands = [
                t for t in all_tasks
                if (t.get("name") == sname or str(t.get("name") or "").startswith(sname + "_"))
                and t.get("output_file") and t.get("downloaded")
            ]
            cands.sort(key=lambda x: str(x.get("submitted_at") or ""))
            if cands:
                picked = selection.get(sname)
                if picked:
                    hit = next(
                        (t for t in cands if t["output_file"].get("filename") == picked),
                        None,
                    )
                    if not hit:
                        raise RuntimeError(f"段「{sname}」指定的版本不存在：{picked}")
                    src_meta.append((sname, hit["output_file"]))
                else:
                    src_meta.append((sname, cands[-1]["output_file"]))
            else:
                missing.append(sname)
        if missing:
            raise RuntimeError(f"以下段还没有视频，无法合成：{'、'.join(missing)}")
    else:
        # 回退：无剧本时按提交顺序拼接所有已下载任务
        for t in all_tasks:
            if t.get("output_file") and t.get("downloaded"):
                src_meta.append((str(t.get("name") or ""), t["output_file"]))
    if not src_meta:
        raise RuntimeError("项目还没有已下载的视频段")
    src_files = []
    for sname, of in src_meta:
        p = os.path.join(OUTPUTS_DIR, of.get("type", "output"), of.get("subfolder", ""), of.get("filename", ""))
        if os.path.isfile(p):
            src_files.append(p)
    if not src_files:
        raise RuntimeError("找不到视频文件（可能未下载）")
    tmpdir = tempfile.mkdtemp(prefix="assemble_")
    norm_segs = []
    skipped = []
    try:
        total_segs = len(src_files)
        for i, src in enumerate(src_files):
            ext = os.path.splitext(src)[1] or ".mp4"
            tmp_src = os.path.join(tmpdir, f"src_{i}{ext}")
            shutil.copy(src, tmp_src)
            seg = os.path.join(tmpdir, f"seg_{i}.mp4")
            if progress_cb:
                progress_cb(i, total_segs)
            cut = detect_lead_noise_sec(tmp_src)
            if cut > 0:
                # 裁剪开头起始音节：音画同步 trim，保留台词前的间隙
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp_src,
                     "-vf", f"trim=start={cut},setpts=PTS-STARTPTS",
                     "-af", f"atrim=start={cut},asetpts=PTS-STARTPTS",
                     "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                     "-c:a", "aac", "-ar", "44100", "-pix_fmt", "yuv420p", seg],
                    capture_output=True,
                )
                if r.returncode == 0 and os.path.exists(seg):
                    print(f"[assemble] 第 {i + 1} 段裁剪开头 {cut:.2f}s")
                    norm_segs.append(seg)
                    continue
            # 无前置音节或裁剪失败：正常转码
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_src, "-c:v", "libx264", "-preset", "fast",
                 "-crf", "20", "-c:a", "aac", "-ar", "44100", "-pix_fmt", "yuv420p", seg],
                capture_output=True,
            )
            if r.returncode != 0 or not os.path.exists(seg):
                skipped.append(src_meta[i][0] if i < len(src_meta) else f"第{i + 1}段")
                print(f"[assemble] 第 {i + 1} 段转码失败，跳过：{r.stderr.decode('utf-8', 'ignore')[-120:]}")
                continue
            norm_segs.append(seg)
        if progress_cb:
            progress_cb(total_segs, total_segs)
        if not norm_segs:
            raise RuntimeError("所有片段都处理失败，无法合成")
        listfile = os.path.join(tmpdir, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for p in norm_segs:
                # Windows 下 ffmpeg concat 需要正斜杠（反斜杠会被当转义）
                f.write(f"file '{p.replace(os.sep, '/')}'\n")
        base = _slug(project_name or "项目") or "project"
        outname = f"合成_{base}{seg_label}_{int(time.time())}.mp4"
        dest = os.path.join(IMAGE_DIRS[0], outname)
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", dest],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.exists(dest):
            raise RuntimeError(f"合成失败：{r.stderr.decode('utf-8', 'ignore')[-200:]}")
        return outname, dest, skipped
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def start_assemble_job(project_name="", selection=None, seg_range=None):
    tid = "asm_" + uuid.uuid4().hex[:10]
    job = {"status": "queued", "message": "准备中", "filename": None, "error": None,
           "done": 0, "total": 0, "skipped": []}
    with ASSEMBLE_JOBS_LOCK:
        ASSEMBLE_JOBS[tid] = job

    def work():
        job["status"] = "running"
        job["message"] = "转码并拼接中…"
        try:
            def _prog(done, total):
                job["done"] = done
                job["total"] = total
                job["message"] = f"处理中 {done}/{total} 段…"

            fn, _, skipped = assemble_project_video(project_name, selection, seg_range, _prog)
            job["filename"] = fn
            job["skipped"] = skipped
            job["status"] = "done"
            job["message"] = f"合成完成{('，跳过 ' + str(len(skipped)) + ' 段') if skipped else ''}"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            job["message"] = "合成失败"
    threading.Thread(target=work, daemon=True).start()
    return tid


# ---------- 图构建与提交 ----------

def build_graphs(tasks):
    if not os.path.isdir(DEFAULT_WORKFLOW_DIR):
        return None, f"找不到工作流目录：{DEFAULT_WORKFLOW_DIR}"
    sys.path.insert(0, DEFAULT_WORKFLOW_DIR)
    try:
        import build_api_graphs as bg
    except Exception as e:
        return None, f"无法加载 build_api_graphs.py：{e}"
    out = []
    for i, t in enumerate(tasks):
        try:
            task = dict(t)
            task.setdefault("id", f"task_{i + 1}_{_slug(t.get('name', ''))}")
            task.setdefault("seed", random.randrange(10 ** 15))
            task.setdefault("mp", 1.0)
            task.setdefault("duration", 10)
            if task.get("chain_waiting"):
                g = None
            elif task["mode"] == "t2v":
                g = bg.convert_t2v(task)
            elif task["mode"] == "i2v":
                if not task.get("image"):
                    return None, f"任务 {task['id']} 是 I2V 模式但未选首帧图"
                g = bg.build_i2v(task)
            elif task["mode"] == "r2v":
                if not task.get("images"):
                    return None, f"任务 {task['id']} 是 R2V 模式但未选参考图"
                g = bg.build_r2v(task)
            else:
                return None, f"未知模式：{task['mode']}"
            out.append((task, g))
        except Exception as e:
            return None, f"构建任务 {t.get('id', t.get('name'))} 失败：{e}"
    return out, None


def _slug(name):
    keep = "".join(ch for ch in str(name) if ch.isalnum() or ch in "_-")
    return (keep or "x")[:20]


def ensure_i2v_prompt(prompt):
    """把用户写的 T2V 三段式提示词改造成 I2VA 首帧参考提示词。"""
    p = str(prompt or "").strip()
    if not p:
        return p
    if "目标视频" in p or "For the target video" in p or "<Picture 1>" in p:
        return p  # 已是参考模式，不改
    header = (
        "目标视频的第 0.00 秒完全参照 <Picture 1>（来自 [Shot 1]）。\n\n"
    )
    body = p.replace(
        "integrated_multimodal_description: [Shot 1]",
        "integrated_multimodal_description: [Shot 1] The subject, lighting, "
        "wardrobe, colors, and framing shown in <Picture 1> carry forward "
        "seamlessly into this shot, keeping the exact same identity and style. ",
        1,
    )
    return header + body


def _extract_fields(prompt):
    """提取 H3 字段：三段式取 integrated_multimodal_description；六段式取 detailed_description。
    返回 (desc, sound, music) 与缺失标记。"""
    p = str(prompt or "").strip()
    m_desc = re.search(r"(?:integrated_multimodal_description|detailed_description):\s*(.+?)(?=\n?\s*overall_soundscape:|\n?\s*non_diegetic_music:|\Z)", p, re.S)
    m_snd = re.search(r"overall_soundscape:\s*(.+?)(?=\n?\s*non_diegetic_music:|\Z)", p, re.S)
    m_mus = re.search(r"non_diegetic_music:\s*(.+?)(?=\Z)", p, re.S)
    return (
        m_desc.group(1).strip() if m_desc else None,
        m_snd.group(1).strip() if m_snd else None,
        m_mus.group(1).strip() if m_mus else None,
    )


SCENE_ANCHORS = [
    {
        "keywords": ["麦田", "田野", "麦浪", "田埂", "wheat", "field"],
        "image": "场景锚点_麦田夜景.png",
        "desc": (
            "月光下的麦田，银绿色麦浪起伏，田埂土路，远处低矮村舍轮廓，薄雾低垂，"
            "1980年代胶片质感，电影级夜景光线，深蓝绿色夜空，柔和月光"
        ),
    },
    {
        "keywords": ["卧室", "床", "台灯", "房间", "bedroom", "bed"],
        "image": "场景锚点_80年代卧室.png",
        "desc": (
            "1980年代中国乡村卧室，雕花木床架，蓝底白花粗布床单，藤编衣柜，黄铜台灯泛光，"
            "青砖墙面，老式铜插销木门，温暖钨丝灯光，怀旧温馨氛围，胶片质感"
        ),
    },
    {
        "keywords": ["教学楼", "学校", "校园", "老槐树", "教室", "school", "campus", "classroom"],
        "image": None,
        "desc": (
            "1980年代中国中学校园，教学楼门口，院子里老槐树，温暖怀旧色调，胶片质感"
        ),
    },
    {
        "keywords": ["办公室", "办公", "写字楼", "工位", "office"],
        "image": None,
        "desc": (
            "深夜办公室内部，零散工位，一盏暖色台灯亮着，落地窗上雨痕蜿蜒，"
            "窗外城市霓虹模糊，电影感光影，胶片质感"
        ),
    },
]


ACTION_ANCHORS = {
    "牵手": "双手缓缓靠近、轻轻握住，手指自然交缠",
    "靠近": "两人距离逐渐缩短，自然地靠近",
    "走近": "以自然不紧不慢的步伐走近",
    "转身": "缓缓转身，身体自然转动，微顿",
    "奔跑": "自然奔跑，衣摆随动作飘动",
    "回头": "回头望向肩后，眼神柔和",
    "拥抱": "双臂环抱，相互贴近，面庞靠近",
    "擦肩": "两人贴身擦肩而过，衣袖短暂相触，一人停步转身",
    "并肩": "并肩而行，步伐自然同步",
    "停下": "停下脚步，转身面向对方",
    "对视": "四目相对，目光停留片刻",
    "低头": "微微低头，带着羞涩的浅笑",
    "伸手": "缓缓伸出手，掌心向上，动作轻柔",
    "walk": "以平稳自然的步伐行走",
    "run": "自然奔跑，衣摆随动作飘动",
    "turn": "缓慢转身，身体自然转动",
    "hold": "双手轻轻握住，手指温柔交缠",
}


# 中文运镜 → 中文运镜句（类型 + 幅度 + 速度）
CAMERA_MOVES = {
    "推近": "镜头以小幅慢速缓缓推近",
    "缓慢推近": "镜头以小幅慢速缓缓推近",
    "推镜": "镜头以中幅慢速向前推",
    "拉远": "镜头以中幅匀速缓缓拉远",
    "缓慢拉远": "镜头以小幅慢速缓缓拉远",
    "横移": "镜头以小幅慢速向左横移",
    "摇": "镜头缓慢摇动",
    "左摇": "镜头缓慢向左摇",
    "右摇": "镜头缓慢向右摇",
    "上摇": "镜头缓慢上摇",
    "下摇": "镜头缓慢下摇",
    "升降": "镜头缓慢升降",
    "固定": "镜头固定不动，机位稳定",
    "静止": "镜头固定不动，机位稳定",
    "固定机位": "镜头固定不动，机位稳定",
    "特写": "镜头对准极近特写",
    "近景": "镜头取近景",
    "中景": "镜头取中景",
    "中近景": "镜头取中近景",
    "远景": "镜头取远景",
    "全景": "镜头取全景",
    "跟拍": "镜头跟随人物运动跟拍",
    "环绕": "镜头围绕人物缓慢环绕",
    "俯拍": "镜头俯拍，高角度",
    "仰拍": "镜头仰拍，低角度",
    "晃动": "镜头轻微晃动",
}

# 场景 → 光线/氛围补句（扩写时附加）
SCENE_DETAILS = {
    "麦田": (
        "月光在麦浪上洒下银色光泽，薄雾低垂，深蓝绿色夜空，静谧而略带寂寥的氛围"
    ),
    "卧室": (
        "钨丝灯暖光在床铺上洒下琥珀色光晕，亲密怀旧氛围，青砖墙上的柔影"
    ),
    "校园": (
        "午后暖阳透过树叶洒落，复古胶片质感，怀旧而温柔的氛围"
    ),
    "办公室": (
        "低调情绪光影，一盏暖色台灯映衬窗外雨痕冷光，城市灯光在窗外模糊，安静紧张的氛围"
    ),
}

# 场景 → 默认环境声（扩写时作为 overall_soundscape）
SCENE_SOUNDS = {
    "麦田": "夜风吹过麦浪的沙沙声，远处虫鸣，隐约的村庄声响。",
    "卧室": "安静的室内环境声，时钟轻响，布料摩擦的细微声，平稳的呼吸声。",
    "校园": "远处模糊的学生说话声，水泥地上的脚步声，树叶沙沙声。",
    "办公室": "规律的键盘敲击声，雨点轻拍窗户声，低沉的办公环境嗡鸣。",
}

# 场景锚点兜底：默认关闭。写死的 1980s 乡村场景库仅适用于特定题材，
# 对灾难/都市等题材会误注入（如把"卧室/床"写进城市洪水镜头）。
ENABLE_SCENE_ANCHOR = False


def _fix_dialogue_tags(desc):
    """对白标签兜底（方案 A 自动化）：把 <d> 内的角色名/动作移到标签外。

    旧格式：<d>[Chinese] 陈默：“用我的。”林夏（垂眸压声）：“考完还你。</d>
    新格式：陈默：<d>[Chinese] 用我的。</d> 林夏（垂眸压声）：<d>[Chinese] 考完还你。</d>
    只处理确实包含说话者冒号的块；已是纯对白的块原样保留。
    """

    def _rebuild(m):
        lang = m.group(1)
        inner = m.group(2).strip()
        if "：" not in inner and ":" not in inner:
            return m.group(0)
        # 段边界：引号闭合后紧跟下一个说话者
        parts = re.split(r'(?<=[”"」』])\s*(?=[\u4e00-\u9fa5A-Za-z0-9])', inner)
        if len(parts) < 2:
            parts = re.split(
                r'(?<=[。！？!?])(?=[\u4e00-\u9fa5A-Za-z0-9]+[（(]?[^：:]*[）)]?[：:])',
                inner,
            )
        out = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if "：" not in part and ":" not in part:
                out.append(f"<d>[{lang}]{part}</d>")
                continue
            m2 = re.match(r"^(.*?)[：:]\s*[“\"「『]?\s*(.*)$", part, re.S)
            if not m2:
                out.append(f"<d>[{lang}]{part}</d>")
                continue
            prose, line = m2.group(1).strip(), m2.group(2).strip()
            line = re.sub(r'[”"」』]+$', "", line).strip()
            if not line:
                out.append(f"<d>[{lang}]{part}</d>")
                continue
            if not line.endswith(("。", "！", "？", "!", "?")):
                line += "。"
            prefix = prose if prose else "人物"
            out.append(f"{prefix}：<d>[{lang}]{line}</d>")
        return " ".join(out) if out else m.group(0)

    return re.sub(r"<d>\[([A-Za-z]+)\](.*?)</d>", _rebuild, str(desc or ""), flags=re.S)


def _assign_speaker_ids(desc, roles=None):
    """把对白前的说话者中文名转成 H3 官方 (S1)/(S2) 稳定 ID。

    例：'陈默低声说：<d>…</d>' → '陈默（S2）低声说：<d>…</d>'
    角色顺序：roles 参数（[{name}]）→ 资产状态表 → 按出现顺序。
    已带（Sx）的提示词整体跳过（幂等）。
    """
    p = str(desc or "")
    if "<d>" not in p:
        return p
    if re.search(r"[（(]\s*S\d+\s*[）)]", p):
        return p
    order = []
    for r in (roles or []):
        n = str(r.get("name") or r.get("role_name") or "").strip()
        if n and n not in order:
            order.append(n)
    if not order:
        try:
            st = load_state()
            order = list((st.get("project") or {}).get("asset_state", {}).get("roles", {}).keys())
        except Exception:
            order = []
    if not order:
        names = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9]{1,8})[：:]\s*<d>", p)
        for n in names:
            if n not in order:
                order.append(n)
    id_map = {n: f"S{i + 1}" for i, n in enumerate(order) if n}
    if not id_map:
        return p

    def _repl(m):
        prose = m.group(1)
        hits = [(n, prose.rfind(n)) for n in id_map if n and n in prose]
        if not hits:
            return m.group(0)
        name, pos = max(hits, key=lambda x: x[1])
        sid = id_map[name]
        return prose[:pos + len(name)] + f"（{sid}）" + prose[pos + len(name):] + "：<d>"

    return re.sub(r"([^<>\n]{1,40}?)[：:]\s*<d>", _repl, p)


def enhance_prompt(prompt, task=None):
    """智能补全 H3 提示词。

    补全项（全部保留用户原文，只加缺失部分）：
    1. I2VA/Ref2VA 对齐指令（i2v/r2v 模式且未写参考对齐时）
    2. 角色锁定句（有参考图且画面描述缺 Identity 锁定时）
    3. 负面约束（人物镜头禁幻影/双曝光/多余人物）
    4. overall_soundscape / non_diegetic_music 缺失时按场景自动补
    5. no_bgm=True 时强制 non_diegetic_music: N/A + 禁配乐约束
    6. 场景锚点自动匹配（关键词 → 场景描述 + 场景参考图标签）
    7. 动作衔接注入（动作词 → 动作描述 + 参考视频标签）
    8. 链式衔接说明（承接上一段末帧）
    """
    task = task or {}
    mode = task.get("mode", "t2v")
    no_bgm = bool(task.get("no_bgm", True))
    images = [x for x in (task.get("images") or []) if x]
    image = task.get("image") or ""
    refs = list(images)
    if image:
        refs.append(image)
    refs = [x for x in dict.fromkeys(refs) if x]
    scene_image = task.get("scene_image") or ""
    ref_video = task.get("ref_video") or ""
    chain_prev = bool(task.get("chain_prev") or task.get("chain_waiting"))

    p = str(prompt or "").strip()
    if not p:
        return p
    # 0. 对白标签兜底：<d> 内只留纯对白（角色名/动作移到标签外）
    p = _fix_dialogue_tags(p)
    # 0b. 说话人 ID：H3 靠 (S1)/(S2) 区分音色，中文名必须转成官方 ID
    p = _assign_speaker_ids(p, task.get("roles") if task else None)

    has_align = "目标视频" in p or "For the target video" in p or "<Picture 1>" in p or "subject_definitions" in p
    desc, snd, mus = _extract_fields(p)

    # 1. 对齐指令：I2V 首帧对齐；R2V 不再注入"Picture 1 首帧"（改由六段式
    #    subject_definitions + keyframe 声明负责，见 to_ref2va_six_section）
    if mode == "i2v" and not has_align and refs:
        header = (
            "目标视频的第 0.00 秒完全参照 <Picture 1>（来自 [Shot 1]）。\n\n"
        )
        p = header + p

    # 2. 角色锁定句（有参考图但描述里没有 identity 锁定词）
    has_lock = any(w in p.lower() for w in ("exact same appearance", "keeps the same", "preserving", "identity"))
    if refs and not has_lock:
        lock = (
            " 人物与参考图保持完全一致的身份、五官、发型、服装与肤色。"
        )
        if desc:
            p = p.replace(desc, desc + lock, 1)
        else:
            p = p + lock

    # 2b. 参考范围声明（inherit / exclude）
    if refs and "只继承" not in p and "inherit" not in p.lower():
        scope = (
            " 参考范围：角色参考图只继承人物身份、发型、服装与肤色，"
            "排除其原图姿势、构图、机位、背景与光线；"
            "场景参考图只继承空间布局、陈设与氛围，排除其原图构图与机位。"
        )
        if desc:
            p = p.replace(desc, desc + scope, 1)
        else:
            p = p + scope

    # 3. 负面约束（有人物/参考图时）
    has_neg = any(w in p.lower() for w in ("no ghosting", "no double exposure", "no extra people"))
    if refs and not has_neg:
        neg = (
            " 画面无重影、无双重曝光、无透明面部、无多余人物、无身份漂移、无文字水印。"
        )
        if desc:
            p = p.replace(desc, desc + neg, 1)
        else:
            p = p + neg

    # 6. 场景锚点自动匹配（默认关闭：写死场景库会误注入不适配题材的环境）
    p_lower = p.lower()
    if ENABLE_SCENE_ANCHOR:
        for anchor in SCENE_ANCHORS:
            hit = any(k.lower() in p_lower for k in anchor["keywords"])
            if not hit:
                continue
            scene_desc = anchor["desc"]
            # 场景参考图标签
            scene_ref = ""
            if scene_image and os.path.basename(scene_image) == anchor["image"]:
                scene_ref = " and matching the scene environment shown in the scene reference picture"
            elif anchor["image"]:
                scene_ref = " and matching the established scene environment"
            if desc:
                p = p.replace(desc, desc + f" 环境为{scene_desc}{scene_ref}。", 1)
            break

    # 6b. 分镜图构图引用：参考图里有分镜图时，明确其构图/机位/人物站位基准作用
    #     （仅非 R2V 模式注入；R2V 由六段式 <Picture N> 故事板声明负责）
    for idx, img in enumerate(refs):
        if mode != "r2v" and (str(img).startswith("分镜_") or str(img).startswith("story_")):
            pic = idx + 1
            comp = (
                f" 本镜构图、机位、景别、人物站位与画面内容严格参照 <Picture {pic}>（分镜图）；"
                "首帧按分镜图构图展开，人物位置与画面布局保持一致。"
            )
            if desc:
                p = p.replace(desc, desc + comp, 1)
            else:
                p = p + comp
            break

    # 9. 对白锁：逐字对白 + 口型边界 + 对白不视觉化
    if "<d>" in p and "lips" not in p_lower and "嘴唇" not in p:
        lock = (
            " 对白锁定：逐字按 <d> 内容发音，不添加语气词或台词外文字；"
            "只在说话者发声期间口型运动，未说话者嘴唇静止；"
            "对白只作为声音信息，不触发闪回、幻象或额外人物。"
        )
        if desc:
            p = p.replace(desc, desc + lock, 1)
        else:
            p = p + lock

    # 9b. 声音角色锁定（方案 A：多说话者按性别区分音色，防全部男声/音色混用）
    if "<d>" in p and ("female voice" not in p_lower and "female" not in p_lower
                       and "男声" not in p and "女声" not in p):
        vlock = (
            " 声音角色锁定：女性角色说话用年轻女性清澈嗓音（clear young female voice），"
            "男性角色说话用低沉年轻男声（low young male voice），两人音色必须明显不同；"
            "台词前无吸气声、无语气词（无嗯/啊/呃）、无杂音、无残响，直接清晰开口。"
        )
        if desc:
            p = p.replace(desc, desc + vlock, 1)
        else:
            p = p + vlock

    # 4/5. 声音字段
    _, snd2, mus2 = _extract_fields(p)
    if snd2 is None:
        default_snd = "安静的自然环境声，轻微环境噪音，清晰的人声对白。"
        p = p.rstrip() + f"\n\noverall_soundscape: {default_snd}"
    if no_bgm:
        if mus2 is not None and mus2.strip().upper() not in ("N/A", "NA", "无", ""):
            p = re.sub(r"non_diegetic_music:\s*.+?(?=\Z)", "non_diegetic_music: N/A", p, flags=re.S)
        elif mus2 is None:
            p = p.rstrip() + "\n\nnon_diegetic_music: N/A"
        # 禁配乐约束
        if "no background music" not in p.lower():
            p = p.rstrip() + (
                "\n\n音频要求：人声对白必须是最突出的声音，干净干爽，"
                "无混响、无回声、无背景音乐、无配乐。"
            )
    else:
        if mus2 is None:
            p = p.rstrip() + "\n\nnon_diegetic_music: 轻柔的环境铺底音，音量低。"

    # 7. 动作衔接注入（放在声音字段之后，避免被字段替换吞掉）
    for kw, action_desc in ACTION_ANCHORS.items():
        if kw in p:
            if ref_video:
                p += (
                    f"\n\n动作参考：动作（{action_desc}）严格参照 <Video 1>，"
                    "匹配其节奏、韵律与镜头运动。"
                )
            else:
                p += (
                    f"\n\n动作指导：动作（{action_desc}）要自然流畅，"
                    "身体运动真实，画面构图保持一致。"
                )
            break

    # 8. 链式衔接说明（承接上一段末帧：人物位置/手部/视线/服装）
    if chain_prev and "continue" not in p_lower and "carry forward" not in p_lower:
        p += (
            "\n\n连续性：本段从上一段末帧无缝延续，保持上一段末帧的人物位置、"
            "手部动作、视线方向、光线与情绪氛围；上一段末帧参考图只继承人物姿态、"
            "手部位置与服装，排除其背景、构图与光线。"
        )

    return p.strip()


def extract_last_frame(video_path):
    """ffmpeg 抽视频最后一帧 → 返回临时 PNG 路径（ASCII 目录）。"""
    tmpdir = tempfile.mkdtemp(prefix="chain_")
    tmp_video = os.path.join(tmpdir, "in.mp4")
    out_png = os.path.join(tmpdir, "last.png")
    shutil.copy(video_path, tmp_video)
    r = subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.1", "-i", tmp_video, "-frames:v", "1", out_png],
        capture_output=True,
    )
    if r.returncode != 0 or not os.path.exists(out_png):
        return None
    return out_png


def submit_tasks(server, tasks, auto_download=True, chain_mode=False, role_images=None, scene_image=None):
    warnings = []
    # R2V 任务：三段式 → 官方六段式（仅本次会直接提交的段；链式等待段保持三段式，
    # 后续由 advance_chain 转 I2V 使用）
    for idx, t in enumerate(tasks):
        if t.get("mode") == "r2v" and (t.get("images") or []):
            is_waiting = bool(t.get("chain_waiting")) or (bool(chain_mode) and idx > 0)
            if not is_waiting:
                t["prompt"] = to_ref2va_six_section(t.get("prompt", ""), t)
    # R2V 模型预检：ref2va 权重缺失时回退 fl2va_pruned 并提示
    r2v_cfg = (_CONFIG.get("models") or {}).get("r2v") or {}
    want_unet = r2v_cfg.get("unet") or "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    want_clip = r2v_cfg.get("clip") or "qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors"
    if any(t.get("mode") == "r2v" and not (
        bool(t.get("chain_waiting")) or (bool(chain_mode) and i > 0)
    ) for i, t in enumerate(tasks)):
        unets, clips = _server_models(server)
        use_unet, use_clip = want_unet, want_clip
        if unets:
            if want_unet not in unets:
                use_unet = "minimax_h3_fl2va_pruned_int8_convrot.safetensors" if "minimax_h3_fl2va_pruned_int8_convrot.safetensors" in unets else unets[0]
                warnings.append(
                    f"⚠️ 服务器模型列表里没有 {want_unet}，R2V 暂回退 {use_unet}（身份锁定弱）。"
                    "文件放对位置后重启 ComfyUI 即可生效。"
                )
        if clips:
            if want_clip not in clips:
                use_clip = clips[0]
                warnings.append(f"⚠️ 缺少 CLIP {want_clip}，R2V 暂用 {use_clip}。")
        for t in tasks:
            if t.get("mode") == "r2v":
                t["r2v_unet"] = use_unet
                t["r2v_clip"] = use_clip
    graphs, err = build_graphs(tasks)
    if err:
        return None, err, warnings
    # I2V 任务先上传首帧图
    for task, _ in graphs:
        if task.get("chain_waiting"):
            continue
        if task["mode"] == "i2v" and task.get("image"):
            lp = find_image(task["image"])
            if lp:
                try:
                    upload_image(server, lp, task["image"])
                except Exception as e:
                    return None, f"上传首帧图 {task['image']} 失败：{e}", warnings
            else:
                return None, f"本地找不到首帧图：{task['image']}", warnings
        if task["mode"] == "r2v":
            for img in (task.get("images") or []):
                lp = find_image(img)
                if not lp:
                    return None, f"本地找不到参考图：{img}", warnings
                try:
                    upload_image(server, lp, img)
                except Exception as e:
                    return None, f"上传参考图 {img} 失败：{e}", warnings

    state = load_state()
    state["server"] = server
    state["auto_download"] = bool(auto_download)
    if role_images:
        state["role_images"] = [r for r in role_images if r]
    if scene_image:
        state["scene_image"] = scene_image
    if role_images or scene_image:
        save_state(state)
    # 链式判定：本次 idx>0，或状态里已有"健康"未结束链 → 新任务追加链尾等待。
    # 死链（等待任务之前已有失败段）不吸收新任务，新任务自成新链。
    has_open_chain = False
    for i, x in enumerate(state["tasks"]):
        if x.get("chain_waiting") and not x.get("prompt_id"):
            dead = any(t.get("error") for t in state["tasks"][:i])
            has_open_chain = not dead
            break
    results = []
    # 查询远程队列：只有上一版还在生成/排队（running/pending）才拦截重提；
    # 已完成/失败/丢失的旧任务可再次提交，版本号从已有最大序号往后接（v2/v3…）
    try:
        _q = api_get(server, "/queue")
        active_pids = {t[1] for t in _q.get("queue_running", [])} | {t[1] for t in _q.get("queue_pending", [])}
        _queue_ok = True
    except Exception:
        _queue_ok = False
    if not _queue_ok:
        # 查不到队列时保守处理：所有已提交的旧任务都视为活跃，避免误重提
        active_pids = {x.get("prompt_id") for x in state["tasks"] if x.get("prompt_id")}

    def _next_task_version(base, tasks):
        """已有 base / base_vN 记录时返回最大版本号（base 本身算 1），无记录返回 0。"""
        maxv = 0
        for x in tasks:
            n = str(x.get("name") or "")
            if n == base:
                maxv = max(maxv, 1)
            else:
                m = re.fullmatch(re.escape(base) + r"_v(\d+)", n)
                if m:
                    maxv = max(maxv, int(m.group(1)))
        return maxv

    prev_new_tid = None
    for idx, (task, g) in enumerate(graphs):
        base_tid = task["id"]
        base_name = str(task.get("name") or base_tid)
        maxv = _next_task_version(base_name, state["tasks"])
        if maxv > 0:
            # 版本化：ID 和名称都带 _vN，链条/删除/去重按唯一版本引用，避免新旧混链
            ver = maxv + 1
            task["name"] = f"{base_name}_v{ver}"
            tid = f"{base_tid}_v{ver}"
        else:
            tid = base_tid
        # 同一段的任意版本还在远程生成/排队 → 拦截（防误重提；等它跑完再提新版本）
        same_family = [
            x for x in state["tasks"]
            if x.get("id") == base_tid or str(x.get("id") or "").startswith(base_tid + "_")
        ]
        if same_family and any(x.get("prompt_id") in active_pids for x in same_family):
            results.append({
                "id": base_tid, "ok": False,
                "error": "上一版还在生成/排队中，请等它完成后再重提",
                "failure_code": "F-DUP-SUBMIT",
            })
            continue
        # 清理同段死链占位（等待中但从未提交成功）：不占远程队列，重提时移除避免挡路/重复
        stale_same = [
            x for x in state["tasks"]
            if (x.get("id") == base_tid or str(x.get("id") or "").startswith(base_tid + "_"))
            and x.get("chain_waiting") and not x.get("prompt_id")
        ]
        if stale_same:
            state["tasks"] = [x for x in state["tasks"] if x not in stale_same]
        is_chain_waiting = bool(task.get("chain_waiting")) or (
            bool(chain_mode) and (idx > 0 or has_open_chain)
        )
        # 链式引用：同一批内严格接"本批上一段的新版本"，不落到旧版本
        chain_prev_ref = prev_new_tid if (bool(chain_mode) and idx > 0 and prev_new_tid) else task.get("chain_prev")
        if not is_chain_waiting:
            try:
                resp = api_post(server, "/prompt", {"prompt": g, "client_id": "batch_console"})
            except Exception as e:
                results.append({
                    "id": base_tid, "ok": False, "error": f"提交失败：{e}",
                    "failure_code": "F-SUBMIT-API",
                })
                continue
        else:
            resp = None
        pid = resp.get("prompt_id") if resp else None
        if not is_chain_waiting and not pid:
            results.append({
                "id": base_tid, "ok": False, "error": f"服务器返回异常：{resp}",
                "failure_code": "F-SUBMIT-RESP",
            })
            continue
        state["tasks"].append({
            "id": tid,
            "name": task.get("name", tid),
            "mode": task.get("mode"),
            "quality": task.get("quality"),
            "steps": task.get("steps"),
            "duration": task.get("duration"),
            "mp": task.get("mp"),
            "prefix": task.get("prefix"),
            "image": task.get("image"),
            "images": task.get("images") or [],
            "story_image": task.get("story_image"),
            "ref_video": task.get("ref_video"),
            "prompt": task.get("prompt", ""),
            "prompt_id": pid,
            "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "downloaded": False,
            "recorded": False,
            "chain_waiting": is_chain_waiting,
            "chain_prev": chain_prev_ref
            or (state["tasks"][-1]["id"] if (chain_mode and idx > 0 and state["tasks"]) else None),
            "chain_done": False,
        })
        results.append({"id": base_tid, "ok": True, "task_id": tid, "prompt_id": pid, "waiting": is_chain_waiting})
        prev_new_tid = tid
    save_state(state)
    return results, None, warnings


# 远程模型列表缓存（ComfyUI object_info，120 秒 TTL）
_MODEL_CACHE = {"t": 0.0, "unets": [], "clips": []}


def _server_models(server):
    """查远程 UNETLoader/CLIPLoader 可用模型名列表（失败返回空列表）。"""
    now = time.time()
    if now - _MODEL_CACHE["t"] < 120 and _MODEL_CACHE["unets"]:
        return _MODEL_CACHE["unets"], _MODEL_CACHE["clips"]
    unets, clips = [], []
    try:
        req = urllib.request.Request(server + "/object_info/UNETLoader", headers={"User-Agent": "batch-console"})
        with _opener().open(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        unets = (d.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name") or [[]])[0]
    except Exception:
        pass
    try:
        req = urllib.request.Request(server + "/object_info/CLIPLoader", headers={"User-Agent": "batch-console"})
        with _opener().open(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        clips = (d.get("CLIPLoader", {}).get("input", {}).get("required", {}).get("clip_name") or [[]])[0]
    except Exception:
        pass
    _MODEL_CACHE.update({"t": now, "unets": unets, "clips": clips})
    return unets, clips


# ---------- 状态查询 / 下载 / 记录 ----------

def download_outputs(server, outputs):
    ok_all = True
    for o in outputs:
        q = urllib.parse.urlencode({
            "filename": o["filename"], "subfolder": o["subfolder"], "type": o["type"],
        })
        dest = os.path.join(OUTPUTS_DIR, o["type"], o["subfolder"], o["filename"])
        os.makedirs(os.path.dirname(dest) or OUTPUTS_DIR, exist_ok=True)
        try:
            req = urllib.request.Request(server + "/view?" + q, headers={"User-Agent": "batch-console"})
            with _opener().open(req, timeout=600) as r, open(dest, "wb") as f:
                f.write(r.read())
        except Exception:
            ok_all = False
    return ok_all


def ensure_records_header():
    if not os.path.exists(RECORDS_CSV):
        with open(RECORDS_CSV, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "name", "mode", "duration_s", "megapixels",
                "elapsed_sec", "status", "outputs", "prompt_id",
            ])
    if not os.path.exists(RECORDS_MD):
        with open(RECORDS_MD, "w", encoding="utf-8") as f:
            f.write(
                "# ComfyUI 批量生成记录\n\n"
                "| 时间 | 名称 | 模式 | 时长 | 分辨率 | 耗时 | 状态 | 输出 |\n"
                "|---|---|---|---|---|---|---|---|\n"
            )


def record_task(server, t, entry):
    st = entry.get("status", {})
    msgs = {m[0]: m[1] for m in st.get("messages", [])}
    t0 = msgs.get("execution_start", {}).get("timestamp")
    t1 = msgs.get("execution_success", {}).get("timestamp")
    elapsed = round((t1 - t0) / 1000) if t0 and t1 else None
    outputs = []
    for nid, o in entry.get("outputs", {}).items():
        for kind in ("images", "videos", "audio"):
            for item in o.get(kind, []):
                outputs.append(item.get("filename"))
    status_str = st.get("status_str", "?")
    ensure_records_header()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with lock:
        with open(RECORDS_CSV, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                ts, t.get("name"), t.get("mode"), t.get("duration"), t.get("mp"),
                elapsed, status_str, ";".join(outputs), t.get("prompt_id"),
            ])
        with open(RECORDS_MD, "a", encoding="utf-8") as f:
            dur = f"{elapsed // 60}分{elapsed % 60:02d}秒" if elapsed else "-"
            f.write(f"| {ts} | {t.get('name')} | {t.get('mode')} | {t.get('duration')}s "
                    f"| {t.get('mp')}MP | {dur} | {status_str} | {'; '.join(outputs) or '-'} |\n")
    return elapsed


def prompt_fingerprint(prompt):
    """提示词版本指纹：只取结构化的短摘要，便于对比"这次改了什么"。"""
    p = str(prompt or "").strip()
    if not p:
        return ""
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


def get_status(server):
    state = load_state()
    tasks = state.get("tasks", [])
    if not tasks:
        return {"server_ok": True, "tasks": []}
    try:
        history = api_get(server, "/history?max_items=200")
        queue = api_get(server, "/queue")
    except Exception as e:
        return {"server_ok": False, "error": str(e), "tasks": tasks}
    running_ids = {t[1] for t in queue.get("queue_running", [])}
    pending_ids = {t[1] for t in queue.get("queue_pending", [])}
    out = []
    changed = False
    for t in tasks:
        pid = t.get("prompt_id")
        if t.get("chain_waiting") and not pid:
            out.append({
                "id": t["id"], "name": t.get("name", t["id"]), "mode": t.get("mode"),
                "duration": t.get("duration"), "mp": t.get("mp"), "prompt_id": None,
                "status": "waiting", "elapsed_sec": None, "outputs": [],
                "error": None, "downloaded": False,
                "submitted_at": t.get("submitted_at", ""),
                "failure_code": t.get("failure_code"),
            })
            continue
        item = {
            "id": t["id"], "name": t.get("name", t["id"]), "mode": t.get("mode"),
            "duration": t.get("duration"), "mp": t.get("mp"), "prompt_id": pid,
            "status": "queued", "elapsed_sec": None, "outputs": [],
            "error": None, "downloaded": t.get("downloaded", False),
            "submitted_at": t.get("submitted_at", ""),
            "failure_code": t.get("failure_code"),
        }
        if pid in pending_ids:
            item["status"] = "queued"
        elif pid in running_ids:
            item["status"] = "running"
        entry = history.get(pid)
        if entry:
            st = entry.get("status", {})
            msgs = {m[0]: m[1] for m in st.get("messages", [])}
            t0 = msgs.get("execution_start", {}).get("timestamp")
            t1 = msgs.get("execution_success", {}).get("timestamp")
            if t0 and t1:
                item["elapsed_sec"] = round((t1 - t0) / 1000)
            if t1:
                item["status"] = "completed"
            elif st.get("status_str") == "error":
                item["status"] = "error"
                item["error"] = st.get("status_str")
            for nid, o in entry.get("outputs", {}).items():
                for kind in ("images", "videos", "audio"):
                    for f in o.get(kind, []):
                        fn = f.get("filename") or ""
                        item["outputs"].append({
                            "kind": kind, "filename": fn,
                            "subfolder": f.get("subfolder", ""), "type": f.get("type", "output"),
                        })
                        if fn.lower().endswith((".mp4", ".webm", ".mov")) and not t.get("output_file"):
                            t["output_file"] = {
                                "filename": fn,
                                "subfolder": f.get("subfolder", ""),
                                "type": f.get("type", "output"),
                            }
                            changed = True
            if item["status"] == "completed" and item["outputs"]:
                if state.get("auto_download") and not t.get("downloaded"):
                    t["downloaded"] = download_outputs(server, item["outputs"])
                    item["downloaded"] = t["downloaded"]
                    changed = True
                if not t.get("recorded"):
                    record_task(server, t, entry)
                    t["recorded"] = True
                    changed = True
        # 服务器重启会清空 ComfyUI 内存里的 history：已完成且本地有产物的任务
        # 一律按"完成"处理，绝不误标 F-LOST；丢失/超时标记只针对没有产物的任务
        if item["status"] not in ("completed", "error") and (t.get("output_file") or t.get("downloaded")):
            item["status"] = "completed"
            if not item["outputs"]:
                of = t.get("output_file") or {}
                item["outputs"] = [{
                    "kind": "videos",
                    "filename": of.get("filename", ""),
                    "subfolder": of.get("subfolder", ""),
                    "type": of.get("type", "output"),
                }]
            item["downloaded"] = bool(t.get("downloaded"))
        # 超时检测：运行/排队超过上限，或既不在队列也不在 history（丢失/被取消）→ 标记失败
        t0 = t.get("submitted_at")
        if t0 and item["status"] not in ("completed", "error") and not (t.get("output_file") or t.get("downloaded")):
            try:
                started = time.mktime(time.strptime(t0, "%Y-%m-%d %H:%M:%S"))
                if time.time() - started > estimate_timeout(t):
                    if pid not in pending_ids and pid not in running_ids and not entry:
                        reason = "lost（远程队列中消失，可能被取消或服务器重启）"
                        fcode = "F-LOST"
                    else:
                        reason = "timeout（生成超时，疑似卡死）"
                        fcode = "F-TIMEOUT"
                    item["status"] = "error"
                    item["error"] = reason
                    t["error"] = reason
                    t["failure_code"] = fcode
                    changed = True
            except Exception:
                pass
        out.append(item)
    changed = advance_chain(server, state) or changed
    if changed:
        save_state(state)
    # 排序：进行中的任务置顶（running > queued > waiting > completed > error），
    # 同状态下最新提交的在前（前端刷新即可看到新任务在最上方）
    def _status_rank(s):
        return {"running": 0, "queued": 1, "waiting": 2, "completed": 3, "error": 4}.get(s, 5)
    out.sort(key=lambda x: str(x.get("submitted_at") or ""), reverse=True)
    out.sort(key=lambda x: _status_rank(x.get("status")))
    return {"server_ok": True, "tasks": out}


def advance_chain(server, state):
    """链式衔接：上一段完成并下载后，抽最后一帧上传，提交下一段。"""
    tasks = state.get("tasks", [])
    changed = False
    by_id = {t.get("id"): t for t in tasks}
    for i, nxt in enumerate(tasks):
        if not nxt.get("chain_waiting") or nxt.get("prompt_id"):
            continue
        # 按 chain_prev 找上一段（支持重新生成指定上一段）；无则取列表前一个
        t = by_id.get(nxt.get("chain_prev")) if nxt.get("chain_prev") else None
        if t is None and i > 0:
            t = tasks[i - 1]
        if t is None:
            continue
        if t.get("chain_done"):
            continue
        if not t.get("prompt_id"):
            continue
        # 上一段失败/超时：标记跳过，链条继续（下一段降级 T2V 或用更早成功帧）
        if t.get("error"):
            t["chain_done"] = True
            t["chain_skipped"] = True
            # 找更早的成功段末帧
            ref = None
            for j in range(i - 1, -1, -1):
                prev = tasks[j]
                if prev.get("output_file") and os.path.exists(
                    os.path.join(OUTPUTS_DIR, prev["output_file"]["type"], prev["output_file"]["subfolder"], prev["output_file"]["filename"])
                ):
                    ref = prev
                    break
            if ref:
                png = extract_last_frame(os.path.join(
                    OUTPUTS_DIR, ref["output_file"]["type"], ref["output_file"]["subfolder"], ref["output_file"]["filename"]
                ))
                if png:
                    chain_img = f"chain_{ref['id']}.png"
                    try:
                        upload_image(server, png, chain_img)
                        task = {
                            "id": nxt["id"], "name": nxt.get("name", nxt["id"]),
                            "mode": "i2v", "prompt": ensure_i2v_prompt(nxt.get("prompt", "")),
                            "quality": nxt.get("quality"),
                            "steps": nxt.get("steps"),
                            "duration": nxt.get("duration", 10), "mp": nxt.get("mp", 1.0),
                            "prefix": nxt.get("prefix", ""), "image": chain_img,
                            "seed": random.randrange(10 ** 15),
                        }
                        sys.path.insert(0, DEFAULT_WORKFLOW_DIR)
                        import build_api_graphs as bg
                        g = bg.build_i2v(task)
                        resp = api_post(server, "/prompt", {"prompt": g, "client_id": "batch_console"})
                        pid = resp.get("prompt_id") if resp else None
                        if pid:
                            nxt["prompt_id"] = pid
                            nxt["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                            nxt["chain_waiting"] = False
                            nxt["image"] = chain_img
                            changed = True
                            print(f"[chain] {t.get('name')} 失败跳过，{nxt.get('name')} 用 {ref.get('name')} 末帧续接（{pid}）")
                            continue
                    except Exception as e:
                        print(f"[chain] 失败跳过续接异常：{e}")
            # 无可用参考帧 → 降级 T2V 直接提交
            try:
                sys.path.insert(0, DEFAULT_WORKFLOW_DIR)
                import build_api_graphs as bg
                task = {
                    "id": nxt["id"], "name": nxt.get("name", nxt["id"]),
                    "mode": "t2v", "prompt": nxt.get("prompt", ""),
                    "duration": nxt.get("duration", 10), "mp": nxt.get("mp", 1.0),
                    "prefix": nxt.get("prefix", ""), "image": "",
                    "seed": random.randrange(10 ** 15),
                }
                g = bg.convert_t2v(task)
                resp = api_post(server, "/prompt", {"prompt": g, "client_id": "batch_console"})
                pid = resp.get("prompt_id") if resp else None
                if pid:
                    nxt["prompt_id"] = pid
                    nxt["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    nxt["chain_waiting"] = False
                    nxt["mode"] = "t2v"
                    nxt["image"] = ""
                    changed = True
                    print(f"[chain] {t.get('name')} 失败跳过，{nxt.get('name')} 降级 T2V 提交（{pid}）")
            except Exception as e:
                print(f"[chain] 降级提交异常：{e}")
            continue
        # 本地视频（未下载则补下载）
        of = t.get("output_file")
        if not of:
            continue
        video_file = os.path.join(OUTPUTS_DIR, of["type"], of["subfolder"], of["filename"])
        if not os.path.exists(video_file):
            q = urllib.parse.urlencode(of)
            try:
                req = urllib.request.Request(server + "/view?" + q, headers={"User-Agent": "batch-console"})
                os.makedirs(os.path.dirname(video_file) or OUTPUTS_DIR, exist_ok=True)
                with _opener().open(req, timeout=600) as r, open(video_file, "wb") as f:
                    f.write(r.read())
            except Exception as e:
                print(f"[chain] 补下载失败：{e}")
                continue
        png = extract_last_frame(video_file)
        if not png:
            print(f"[chain] 抽帧失败：{t.get('name')}")
            continue
        chain_img = f"chain_{t['id']}.png"
        try:
            upload_image(server, png, chain_img)
        except Exception as e:
            print(f"[chain] 上传失败：{e}")
            continue
        # 下一段：链式参考图 = 上段末帧（P1，首帧对齐）+ 本段场景 + 角色锚点。
        # 去掉分镜图：分镜图场景可能与上段末帧不同，混在一起会让 H3 生成场景切换
        base_refs = [x for x in (nxt.get("images") or []) if x and not str(x).startswith("chain_")]
        ref_imgs = [x for x in base_refs if not (str(x).startswith("分镜_") or str(x).startswith("story_"))]
        ref_imgs = list(dict.fromkeys(ref_imgs[:3]))
        if chain_img not in ref_imgs:
            ref_imgs.insert(0, chain_img)  # 链帧放第一位：首帧对齐 + 构图延续
        # 链式提示词：首帧延续上一段末帧，整段保持单一场景
        chain_prompt = str(nxt.get("prompt") or "")
        if chain_prompt and "不切换场景" not in chain_prompt:
            chain_prompt = (
                " 首帧严格延续上一段末帧的场景、人物位置与光线；"
                "整段画面保持单一场景，不出现场景切换、不出现其他地点。"
            ).join([chain_prompt, ""]) if False else chain_prompt + (
                " 首帧严格延续上一段末帧的场景、人物位置与光线；"
                "整段画面保持单一场景，不出现场景切换、不出现其他地点。"
            )
        # 关键：waiting 任务从未提交过，其参考图（锚点/场景/分镜）还没上传到远程，
        # 必须在上传链帧之外把所有本地参考图也上传，否则 POST /prompt 会 400
        for img in ref_imgs:
            lp = find_image(img)
            if lp:
                try:
                    upload_image(server, lp, img)
                except Exception as e:
                    print(f"[chain] 上传参考图失败 {img}: {e}")
        sys.path.insert(0, DEFAULT_WORKFLOW_DIR)
        try:
            import build_api_graphs as bg
        except Exception as e:
            print(f"[chain] 加载 build_api_graphs 失败：{e}")
            continue
        # 链式衔接统一用 I2V：上段末帧作为本段首帧图，画面从上帧直接发展（最连贯）。
        # R2V 多参考下 H3 不保证从链帧开始，段间会跳变。
        task = {
            "id": nxt["id"],
            "name": nxt.get("name", nxt["id"]),
            "mode": "i2v",
            "prompt": ensure_i2v_prompt(chain_prompt),
            "quality": nxt.get("quality"),
            "steps": nxt.get("steps"),
            "duration": nxt.get("duration", 10),
            "mp": nxt.get("mp", 1.0),
            "prefix": nxt.get("prefix", ""),
            "image": chain_img,
            "story_image": nxt.get("story_image"),
            "ref_video": nxt.get("ref_video"),
            "seed": random.randrange(10 ** 15),
        }
        build_fn = bg.build_i2v
        try:
            g = build_fn(task)
            resp = api_post(server, "/prompt", {"prompt": g, "client_id": "batch_console"})
        except Exception as e:
            print(f"[chain] 提交下一段失败：{e}")
            continue
        pid = resp.get("prompt_id") if resp else None
        if not pid:
            print(f"[chain] 下一段提交异常：{resp}")
            continue
        nxt["prompt_id"] = pid
        nxt["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        nxt["chain_waiting"] = False
        nxt["image"] = chain_img
        nxt["images"] = []
        nxt["mode"] = "i2v"
        t["chain_done"] = True
        changed = True
        print(f"[chain] {t.get('name')} → {nxt.get('name')} 已提交（{pid}）")
    return changed


def check_server(server):
    try:
        stats = api_get(server, "/system_stats", timeout=8)
        queue = api_get(server, "/queue", timeout=8)
        return {
            "ok": True,
            "system": stats.get("system", {}),
            "queue_running": len(queue.get("queue_running", [])),
            "queue_pending": len(queue.get("queue_pending", [])),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- CSV / JSON 导入导出 ----------

CSV_FIELDS = ["name", "mode", "duration", "mp", "image", "images", "prefix", "prompt"]


def parse_time_range(s):
    """从 Prompt 标题时间范围（0:00-0:15 / 00:00-00:15）解析秒数。"""
    m = re.search(r"(\d+):(\d+)\s*[-–—]\s*(\d+):(\d+)", s)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    return (h2 * 60 + m2) - (h1 * 60 + m1)


def _renumber_shots(body):
    """把全局 [Shot N] 编号重排为从 1 开始（每段是独立任务）。"""
    counter = 0
    def repl(m):
        nonlocal counter
        counter += 1
        return f"[Shot {counter}]"
    return re.sub(r"\[Shot\s*\d+\]", repl, body)


def _add_shot_timestamps(body, duration):
    """段内多镜头且无时间戳时，按时长均分补 At MM:SS.mmm。"""
    shots = re.findall(r"\[Shot\s*\d+\]", body)
    if len(shots) < 2 or re.search(r"At\s+\d+:\d+", body):
        return body
    n = len(shots)
    out = []
    shot_idx = 0
    for seg in re.split(r"(\[Shot\s*\d+\])", body):
        if re.match(r"^\[Shot", seg):
            shot_idx += 1
            if shot_idx > 1:
                t = duration * (shot_idx - 1) / n
                mm = int(t // 60)
                ss = int(t % 60)
                ms = int(round((t - int(t)) * 1000))
                out.append(f"{seg} At {mm:02d}:{ss:02d}.{ms:03d}")
            else:
                out.append(seg)
        else:
            out.append(seg)
    return "".join(out)


def optimize_prompt_block(body, duration):
    """把对话导出的 Prompt 块规范成 H3 任务提示词。

    - 剥掉 Copy 等无效标记（调用方已处理）
    - 全局 [Shot N] → 每段独立任务从 [Shot 1] 开始
    - 三大字段顺序固定：integrated_multimodal_description → overall_soundscape → non_diegetic_music
    - 段内多镜头无时间戳时按时长均分补 At
    """
    p = str(body or "").strip()
    if not p:
        return p
    # 三字段提取：非贪婪 lookahead，兼容 overall_soundscape 与上一行粘连
    m_desc = re.search(r"integrated_multimodal_description:\s*(.+?)(?=\n?\s*overall_soundscape:|\Z)", p, re.S)
    m_snd = re.search(r"overall_soundscape:\s*(.+?)(?=\n?\s*non_diegetic_music:|\Z)", p, re.S)
    m_mus = re.search(r"non_diegetic_music:\s*(.+?)(?=\Z)", p, re.S)
    if not (m_desc and m_snd and m_mus):
        return _renumber_shots(p)  # 结构不完整，只重排镜头号
    desc = _renumber_shots(m_desc.group(1).strip())
    desc = _add_shot_timestamps(desc, duration)
    return (
        f"integrated_multimodal_description: {desc}\n\n"
        f"overall_soundscape: {m_snd.group(1).strip()}\n\n"
        f"non_diegetic_music: {m_mus.group(1).strip()}"
    )


def parse_prompt_blocks(text):
    """把对话导出的 Prompt 块文本分成任务列表。

    输入示例：
    形态 A（带标题）：
        雨夜加班（第 1-10 段）
        Prompt 1（0:00-0:15）

        Copy
        integrated_multimodal_description: [Shot 1] ...
        overall_soundscape: ...
        non_diegetic_music: ...

        Prompt 2（0:15-0:30）
        ...

    形态 B（无标题，直接粘贴多段三段式）：
        integrated_multimodal_description: [Shot 1] ...
        overall_soundscape: ...
        non_diegetic_music: ...

        integrated_multimodal_description: [Shot 2] ...

    - "Copy" 是复制界面标记，属于无效词，直接剥掉
    - 按 Prompt N 切块，标题里的时间范围作为时长
    - 无标题时按 integrated_multimodal_description 开头切纯 prompt 块
    """
    title_m = re.search(r"^\s*(.+?)(?:（第\s*\d+\s*[-–—]\s*\d+\s*段）|\(.*?\))?\s*$", text, re.M)
    base_name = ""
    if title_m and not title_m.group(1).startswith("integrated_multimodal_description"):
        base_name = title_m.group(1).strip()
    base_name = base_name or "批量任务"
    base_slug = _slug(base_name) or "batch"
    rows = []

    # 形态 A：带 Prompt N 标题
    parts = re.split(r"(?=Prompt\s*\d+)", text)
    for part in parts:
        pm = re.match(r"Prompt\s*(\d+)\s*(?:[（(]\s*([^）)]*?)\s*[）)])?", part)
        if not pm:
            continue
        body = _strip_prompt_wrapper(part)
        if not body:
            continue
        dur = parse_time_range(pm.group(2)) if pm.group(2) else 15
        idx = int(pm.group(1))
        rows.append({
            "name": f"{base_name}_{idx:02d}",
            "mode": "t2v",
            "duration": dur,
            "mp": 1.0,
            "image": "",
            "prefix": f"video/{base_slug}_{idx:02d}",
            "prompt": optimize_prompt_block(body, dur),
        })
    if rows:
        return rows

    # 形态 B：无标题，按 integrated_multimodal_description 块切分
    blocks = re.split(r"(?=integrated_multimodal_description:)", text)
    idx = 0
    for blk in blocks:
        body = _strip_prompt_wrapper(blk)
        if not body or not body.startswith("integrated_multimodal_description"):
            continue
        idx += 1
        dur = _infer_prompt_duration(body)
        rows.append({
            "name": f"{base_name}_{idx:02d}",
            "mode": "t2v",
            "duration": dur,
            "mp": 1.0,
            "image": "",
            "prefix": f"video/{base_slug}_{idx:02d}",
            "prompt": optimize_prompt_block(body, dur),
        })
    return rows


def _strip_prompt_wrapper(part):
    """剥掉 Prompt 标题行、Copy 无效标记，返回纯提示词正文。"""
    body_lines = []
    for ln in part.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.lower() == "copy":
            continue
        if re.match(r"^Prompt\s*\d+", s):
            continue
        body_lines.append(s)
    return "\n".join(body_lines).strip()


def _infer_prompt_duration(body):
    """无标题块：按块内最后一个 At MM:SS.mmm 时间戳推时长（+2s 余量）。"""
    times = re.findall(r"At\s+(\d+):(\d+)(?:\.\d+)?", body)
    if times:
        last = max(int(m) * 60 + int(s) for m, s in times)
        return max(5, min(30, last + 2))
    return 15


def parse_tasks_text(text, fmt):
    rows = []
    if fmt == "json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("tasks", [])
        for i, r in enumerate(data):
            rows.append(_normalize_row(r, i + 1))
    elif fmt == "prompts":
        rows = parse_prompt_blocks(text)
    else:
        reader = csv.DictReader(io.StringIO(text))
        for i, r in enumerate(reader):
            if not (r.get("prompt") or "").strip():
                continue
            rows.append(_normalize_row(r, i + 1))
    return rows


def _normalize_row(r, idx):
    images_raw = str(r.get("images") or "").strip()
    return {
        "name": str(r.get("name") or "").strip() or f"任务{idx}",
        "mode": str(r.get("mode") or "t2v").strip(),
        "duration": int(float(r.get("duration") or 10)),
        "mp": float(r.get("mp") or 1.0),
        "image": str(r.get("image") or "").strip(),
        "images": [x.strip() for x in images_raw.split("|") if x.strip()] if images_raw else [],
        "prefix": str(r.get("prefix") or "").strip() or f"video/H3_batch_{idx}",
        "prompt": str(r.get("prompt") or "").strip(),
    }


# ---------- 剧本 JSON 导入（Novel-Director / ArcReel / 通用分镜） ----------

# 角色关键词 → 推荐锚点图。可自行增补，匹配到会作为链式 R2V 参考图。
ROLE_IMAGE_HINTS = [
    (["男孩", "男主", "少年", "male", "boy", "林深"], "角色锚点_男孩_1980s.png"),
    (["女孩", "女主", "少女", "female", "girl", "苏晚"], "角色锚点_女孩_1980s.png"),
]


def _pick(obj, *keys):
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", []):
            return v
    return None


def match_role_image(role_name, role_desc=""):
    """角色 → 本地参考图。

    匹配顺序：项目锚点图（关键词表）→ 文件名包含角色全名（≥2字）。
    找不到返回 None（该角色将降级 T2V 生成）。
    """
    text = f"{role_name} {role_desc}"
    for kws, img in ROLE_IMAGE_HINTS:
        if any(k.lower() in text.lower() for k in kws) and find_image(img):
            return img
    name = str(role_name).strip()
    if len(name) >= 2:
        for fn in list_images():
            if name.lower() in os.path.splitext(fn)[0].lower():
                return fn
    return None


def match_scene(scene_name):
    """场景名 → SCENE_ANCHORS 锚点（返回 dict 或 None）。"""
    s = str(scene_name or "").lower()
    for anchor in SCENE_ANCHORS:
        if any(k.lower() in s for k in anchor["keywords"]):
            return anchor
    return None


def _extract_roles(data):
    """从剧本里提取角色表 → {角色名: {role_name, role_desc, avatar}}。"""
    role_map = {}
    raw = _pick(data, "role_list", "roles", "characters", "role_definitions") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    for r in raw:
        if isinstance(r, str):
            r = {"role_name": r}
        if not isinstance(r, dict):
            continue
        name = str(_pick(r, "role_name", "name", "character", "id") or "").strip()
        if not name:
            continue
        role_map[name] = {
            "role_name": name,
            "role_desc": str(_pick(r, "role_desc", "description", "desc", "character_desc") or "").strip(),
            "avatar": str(_pick(r, "avatar", "image", "portrait", "ref_image", "picture") or "").strip(),
        }
    return role_map


def _extract_storyboards(data):
    """从剧本里提取分镜列表（兼容多种键名）。"""
    raw = _pick(
        data, "storyboard_list", "storyboards", "segments", "scenes", "shots", "tasks"
    ) or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    out = []
    for i, sb in enumerate(raw):
        if isinstance(sb, str):
            out.append({"index": i + 1, "prompt": sb})
        elif isinstance(sb, dict):
            item = dict(sb)
            item.setdefault("index", i + 1)
            out.append(item)
    return out


def _sb_roles(sb):
    """分镜里的角色列表（兼容字符串/数组/对象数组）。"""
    raw = _pick(sb, "role_list", "roles", "characters", "actors")
    if not raw:
        return []
    if isinstance(raw, str):
        return [x.strip() for x in re.split(r"[、,，/]", raw) if x.strip()]
    names = []
    for x in raw:
        if isinstance(x, dict):
            nm = _pick(x, "role_name", "name", "id")
            if nm:
                names.append(str(nm).strip())
        else:
            names.append(str(x).strip())
    return [n for n in names if n]


def _sb_duration(sb, default=10):
    """分镜时长：duration 字段 > 标题时间范围 > 默认 10s。"""
    dur = _pick(sb, "duration", "seconds", "length")
    if dur is not None:
        try:
            return max(1, min(30, int(float(dur))))
        except (TypeError, ValueError):
            pass
    t = _pick(sb, "time_range", "range")
    if t:
        p = parse_time_range(str(t))
        if p:
            return max(1, min(30, p))
    return default


_DIALOGUE_LINE_RE = re.compile(
    r"([\u4e00-\u9fff]{1,8})(?:（([^）]*)）)?[：:]\s*[\"“](.*?)"
    r"(?=[\u4e00-\u9fff]{1,8}(?:（[^）]*）)?[：:][\"“]|\u201d|$)",
    re.S,
)


def split_dialogue_lines(text):
    """把「角色（动作）：“对白。”」拆成 (角色, 动作, 纯对白) 列表。

    方案 A：角色名与动作神态放 <d> 之外，<d>[Chinese] 里只保留纯对白字面。
    拆不出时返回空列表，由调用方按原文兜底。
    """
    text = (text or "").strip()
    if not text:
        return []
    lines = []
    for m in _DIALOGUE_LINE_RE.finditer(text):
        sp = m.group(1).strip()
        act = (m.group(2) or "").strip()
        speech = m.group(3).strip().rstrip("\u201d\"。，；;")
        if sp and speech:
            lines.append((sp, act, speech))
    return lines


def compose_storyboard_prompt(sb, role_map, detailed=False):
    """剧本要素 → H3 三段式提示词。

    detailed=True 时深度扩写：运镜词典、场景光线氛围、动作细节、负面约束、
    声音设计全部展开，适合「剧本简略 → 可执行分镜」。
    """
    scene_name = str(_pick(sb, "scene", "location", "place", "scene_name") or "").strip()
    anchor = match_scene(scene_name) if scene_name else None
    scene_desc = anchor["desc"] if anchor else f"a 1980s Chinese setting, {scene_name or 'rural China'}"
    scene_key = None
    for k in SCENE_DETAILS:
        if k in scene_name:
            scene_key = k
            break

    role_parts = []
    for rn in _sb_roles(sb):
        r = role_map.get(rn)
        if r and r.get("role_desc"):
            role_parts.append(f"{rn}（{r['role_desc']}）")
        else:
            role_parts.append(rn)
    action = str(_pick(sb, "action", "motion", "action_desc") or "").strip()
    dialogue = str(_pick(sb, "dialogue", "line", "speech", "台词") or "").strip()
    camera = str(_pick(sb, "camera", "camera_move", "shot_type") or "").strip() or "缓慢推近"

    parts = [f"环境为{scene_desc}"]
    if detailed and scene_key:
        parts.append(SCENE_DETAILS[scene_key])
    if role_parts:
        parts.append("画面人物：" + "、".join(role_parts))
    if detailed and role_parts:
        parts.append("人物保持与参考图完全一致的身份、发型、服装与肤色，神态自然、情绪真实")
    if action:
        # 动作细节：命中词典用英文动作句，否则保留用户原文
        action_detail = ""
        for kw, a_desc in ACTION_ANCHORS.items():
            if kw in action:
                action_detail = a_desc
                break
        if detailed and action_detail:
            parts.append(f"动作：{action_detail}")
        else:
            parts.append(f"动作：{action}")
    if dialogue:
        dlines = split_dialogue_lines(dialogue)
        if dlines:
            spoken = "".join(
                f"{sp}{('（' + act + '）') if act else ''}：<d>[Chinese] {speech}</d>"
                for sp, act, speech in dlines
            )
            parts.append(f"人物用中文清晰说出对白：{spoken}")
        else:
            parts.append(f"人物用中文清晰说出对白：<d>[Chinese] {dialogue}</d>")
    if detailed:
        camera_verb = None
        for kw, cv in CAMERA_MOVES.items():
            if kw in camera:
                camera_verb = cv
                break
        parts.append((camera_verb or "The camera frames a medium shot") + "。")
    else:
        parts.append(f"镜头{camera}")
    if detailed and role_parts:
        parts.append(
            "画面无重影、无双重曝光、无透明面部、无多余人物、无身份漂移，"
            "无彩带、无花瓣、无丝带、无闪光纸屑、无漂浮粒子，无文字水印"
        )
    if detailed:
        desc = ". ".join(p.rstrip("。，") for p in parts if p) + "."
    else:
        desc = "，".join(parts) + "。"

    snd = str(_pick(sb, "sound", "soundscape", "sound_effects", "audio") or "").strip()
    if not snd:
        if detailed and scene_key:
            snd = SCENE_SOUNDS[scene_key]
        else:
            snd = "自然的环境声，清晰的人声对白，无背景音乐。"
    mus = str(_pick(sb, "music", "non_diegetic_music") or "").strip() or "N/A"
    return (
        f"integrated_multimodal_description: [Shot 1] {desc}\n\n"
        f"overall_soundscape: {snd}\n\n"
        f"non_diegetic_music: {mus}"
    )


def expand_script_json(text):
    """剧本 JSON → 深度扩写后的任务行 + 元信息（同 parse_script_json）。

    与 parse_script_json 的区别：每个分镜的提示词都按 detailed 模式扩写；
    分镜自带 prompt 时保留原文并调用 enhance_prompt 补全缺失项。
    """
    rows, meta = parse_script_json(text)
    if not rows:
        return rows, meta
    data = json.loads(_clean_json_text(text))
    if isinstance(data, list):
        data = {"title": "剧本", "storyboards": data}
    role_map = _extract_roles(data)
    storyboards = _extract_storyboards(data)
    if not storyboards:
        return rows, meta
    for i, (sb, row) in enumerate(zip(storyboards, rows)):
        own = str(_pick(sb, "prompt", "video_prompt", "text", "description") or "").strip()
        if own:
            row["prompt"] = enhance_prompt(optimize_prompt_block(own, row["duration"]), row)
        else:
            row["prompt"] = compose_storyboard_prompt(sb, role_map, detailed=True)
        row["name"] = row["name"]
    return rows, meta


H3_EXPAND_SYSTEM = """你是资深短视频分镜导演，精通 MiniMax H3 视频模型的提示词规范。
任务：把用户提供的每个简略分镜扩写成完整可执行的 H3 三段式提示词。

硬性规则：
1. integrated_multimodal_description 以 [Shot 1] 开头，写可执行的画面描述，必须包含：
   - 开头风格标注（Live-action, cinematic, vintage 1980s Chinese film grain 等，按剧本时代）
   - 详细环境（空间布局、物件、天气）、光线（光源、色温、明暗）、色调氛围
   - 人物外貌、服装、发型、状态（必须强调与参考图完全一致）
   - 具体动作：谁做什么、怎么做、幅度节奏（人物不干站，必须有自然动作）
   - 情绪表演：表情、眼神、呼吸、细微肢体
   - 运镜：类型+幅度+速度自然句（如 The camera pushes in with small amplitude at slow speed）
   - 对白逐字保留：<d>[Chinese] 原文</d>
   - 负面约束：No ghosting, no double exposure, no extra people, no identity drift, no floating particles, no text or watermark
2. overall_soundscape：1-4 句英文，环境声+动作声+非语言人声，不重复对白内容
3. non_diegetic_music：一律 N/A（本剧不要背景音乐）
4. 三大字段顺序固定，字段间空一行

补全边界：
- 可以补充环境、光线、动作细节、镜头语言、情绪状态、声音设计
- 对白：在保持原意、不改变剧情事件和人物关系的前提下，允许理顺语言组织、
  修正病句与前言不搭后语、补足自然称呼和语气词，让对话前后呼应、符合人物
  性格与时代背景；不得改变对白想表达的核心意思，不得新增剧情事件
- 不得发明剧本没有的人物

只输出一个 JSON 数组，格式：[{"id": 1, "prompt": "integrated_multimodal_description: ...\\n\\noverall_soundscape: ...\\n\\nnon_diegetic_music: N/A"}, ...]
不要输出任何其他文字、解释或 markdown 代码块标记。"""


H3_POLISH_SYSTEM = """你是资深短视频编剧兼 MiniMax H3 视频模型提示词专家。
任务：优化用户提供的各段 H3 提示词，重点理顺对白逻辑与语言组织。

允许：
- 修正对白前言不搭后语、逻辑不通、病句、语序混乱
- 理顺两句对白之间的衔接，补足自然的称呼、语气词，符合人物性格与时代背景
- 对白用自然流畅的中文口语重写，保持原意与情绪基调

禁止：
- 改变剧情事件、人物关系、场景、动作、镜头设计
- 增加新情节、新人物或改变故事走向
- 删除或改变画面描述中的关键信息
- 改动三大字段结构、顺序和 non_diegetic_music 的值（保持 N/A）

对白必须仍用 <d>[Chinese] 优化后的原文</d> 包裹。
只输出 JSON 数组：[{"id": 1, "prompt": "integrated_multimodal_description: ...\\n\\noverall_soundscape: ...\\n\\nnon_diegetic_music: N/A"}, ...]
不要输出任何其他文字、解释或 markdown 代码块标记。"""


SCRIPT_GEN_SYSTEM = """你是资深短剧编剧，擅长 1980s 中国乡村/校园题材的细腻叙事。
任务：根据用户提供的题材、梗概和风格要求，创作一部可逐段拍摄的短剧剧本。

剧本结构必须输出 JSON：
{
  "title": "剧名",
  "logline": "一句话梗概",
  "role_list": [
    {"role_name": "角色名", "role_desc": "外貌/服装/性格，一句话"}
  ],
  "storyboard_list": [
    {
      "id": 1,
      "scene": "场景（如：麦田夜景 / 教室 / 卧室）",
      "roles": ["角色名"],
      "action": "这段发生什么动作（具体、可拍摄）",
      "dialogue": "对白（自然口语，中文）",
      "emotion": "情绪基调（一句话）",
      "camera": "运镜（如：缓慢推近 / 固定中景）",
      "duration": 10
    }
  ]
}

要求：
- 每段 5-15 秒，故事有起承转合，10 段左右（按用户要求的段数）
- 黄金开场：第一段开场即高能（冲突、危机、情绪爆发），不做平淡铺垫
- 节奏公式：3 秒钩子开场 + 冲突推进 + 情绪起伏（过山车）+ 结尾留钩，段与段之间有因果链（因为→所以）
- 对白即行动：每句对白有议程（争取/试探/逼迫/回避），有潜台词，人物声音可区分，台词短而有力、有金句
- 动作具体可拍摄：谁、做什么、怎么做；内心戏转成可拍的表情/动作/细节
- 场景从农村/校园生活取：麦田、土路、教室、卧室、村口、晒谷场等
- 不得使用现代元素（手机、网络、汽车等）
- 只输出 JSON，不要任何其他文字或 markdown 代码块标记"""


SCRIPT_REWRITE_SYSTEM = """你是爆款短剧资深编剧。用专业方法论改编用户提供的短剧剧本，
让每一段更抓人、更可拍、更有情绪张力，同时从编剧角度理顺人物动作与情节的流畅性和合理性。

【保留边界】核心剧情事件、人物、人物关系、故事走向、时代背景、场景类型不变，不新增人物。
在这条边界内，你可以放手做结构级改编。

【允许的结构级操作】
- 合并：两段或更多冗余/断裂的段合并成一段，删过渡废话，让事件更紧凑
- 拆分：一段塞了太多信息/节奏过密，拆成两段，每段只讲一件事
- 微调节奏：调整段落时长与内部节奏（铺垫缩短、爆发延长），让情绪张弛有度
- 理顺顺序：段落之间保持因果逻辑（因为→所以），若某段明显顺序颠倒可调整
- 重设段落边界：让每段"进入得晚、退出得早"，结尾都停在状态变化或情绪点上

【改写检查表——每段依次过一遍】
1. 钩子（每段开头）：开场即高能，不铺垫不废话。把最刺激的瞬间（冲突、危机、反转、情绪爆发）提到段首。
2. 冲突（每段中段）：这段谁想要什么、谁在挡、观众能看到什么行动？没有冲突就造张力（试探、逼迫、隐瞒、争夺）。
3. 转向（每段结尾）：结尾必须有状态变化或情绪最高点，不能平收；段与段之间埋钩（悬念/危机/下一股压力）。
4. 因果链：每段行动必须导致下一段反应，禁止"然后发生"式的平铺。
5. 情绪过山车：整片情绪有起伏和反差（甜→虐、平静→爆发、希望→绝望），不能一个调子到底。
6. 对白即行动：每句对白有议程（争取/试探/逼迫/回避/划清界限/重新定义关系），禁止"角色把剧情读给观众"。
7. 潜台词：重要的对话话里有话，不直白说破。
8. 人物声音区分：不同角色说话方式不同（身份、性格、时代背景），不能一个味。
9. 内心戏可视化：把"她心里难过"改成可拍的动作/表情/细节（指尖攥紧、眼眶泛红、别过头去）。
10. 台词打磨：短、有力量、口语化、偶尔有金句/热梗，删掉口水话和客套。
11. 可拍性：每个动作具体到演员能执行（"她瞪他"可以，"她内心复杂"不行）。

【输出要求】
输出与输入相同的 JSON 结构（title / logline / role_list / storyboard_list），
storyboard_list 每项字段不变（id / scene / roles / action / dialogue / emotion / camera / duration）。
id 从 1 连续重排。只输出 JSON，不要任何其他文字或 markdown 代码块标记。"""


SCRIPT_TEXT_SYSTEM = """你是资深短剧编剧。把用户提供的剧本片段/小说片段/剧情文字，
转换成可逐段拍摄的结构化短剧剧本。

要求：
- 保持原有人物、对白、情节事件不变，不新增剧情、不改变故事走向
- 对白语言组织理顺，符合人物性格与时代背景
- 拆分为每段 5-15 秒的分镜，段与段之间动作衔接自然
- 补全：场景、动作细节、情绪基调、运镜、时长
- 只输出 JSON，不要任何其他文字或 markdown 代码块标记

JSON 结构：
{
  "title": "剧名",
  "logline": "一句话梗概",
  "role_list": [{"role_name": "角色名", "role_desc": "外貌/服装/性格"}],
  "storyboard_list": [
    {"id": 1, "scene": "场景", "roles": ["角色名"], "action": "动作",
     "dialogue": "对白", "emotion": "情绪", "camera": "运镜", "duration": 10}
  ]
}"""


ASSET_PROMPT_SYSTEM = """你是文生图提示词专家。根据用户的修改要求，修改给定的图片生成提示词。

要求：
- 保持原提示词的主体风格（时代、质感、画幅）与负面约束（防穿帮）
- 按用户要求精准增删改：人物外貌、服装、发型、性别、动作、场景、光线、色调等
- 必须保留防穿帮约束：无多余肢体、无多余手指、无重复人物、无文字乱码、无现代物品
- 输出修改后的完整提示词（一段中文+英文混合即可），只输出提示词本身，不要任何解释或引号"""


STORY_PROMPT_SYSTEM = """你是爆款短剧分镜画师兼文生图提示词专家。根据剧本某一分镜的情节，生成符合规范的文生图提示词（用于生成分镜图）。

【标准结构——按顺序写】
1. 场景环境：地点 + 时间（日/夜/清晨/黄昏）+ 天气 + 光线来源与色调
2. 角色形象：每个在场角色严格保持角色设定（性别/年龄/发型/服装），一字不差
3. 情节动作：本段核心动作，具体可拍：谁、做什么、怎么动
4. 情绪氛围：把情绪转成视觉（光线冷暖、色调、气氛词、构图松紧），禁止抽象情绪词
5. 构图：按运镜给出画面构图（特写/中近景/双人同框/远景），竖构图 3:4
6. 风格：1980s 中国乡村，电影感故事板，写实，胶片质感
7. 负面约束（必须保留）：画面中严格只有 N 个剧本角色，无第三人、无多余的肢体/手臂/手从画面外伸入，每人四肢完整各两只手、手指清晰不粘连，服装道具正常无穿帮，无文字乱码，无现代物品，无塑料包装盒，无饮料瓶

【角色形象一致性】每个角色的性别、发型、服装必须与提供的"角色锚点提示词"完全一致，全片所有分镜图同一角色形象不变。

【情绪 → 视觉映射参考】
- 紧张/压抑（考场、对峙）：午后斜光、冷色调、构图收紧、阴影重
- 甜蜜/心动：黄昏暖光、柔焦、暖色调、光晕
- 离别/不舍：清晨薄雾、冷青色调、空气感
- 夜晚/暧昧：月光冷调 + 暖色点光、深蓝夜色
- 温暖/治愈：暖阳、米黄色调、柔光

【输出要求】只输出一段完整的生图提示词（中文为主），包含上述 7 个部分，不要任何解释、标题或引号。"""


FAILURE_CODES_SYSTEM = """# 失败码诊断速查表

生成失败先定位责任层，不要整体重跑。每轮只改一个变量；同一错误连续两批无改善就回上层（资产/镜头契约/平台适配），不继续抽卡。

## 提交/运行层

| 失败码 | 症状 | 责任层 | 最小修复 |
|---|---|---|---|
| F-SUBMIT-API | 提交 /prompt 接口失败 | 网络/服务 | 检查远程 ComfyUI 地址与状态后重试 |
| F-SUBMIT-RESP | 服务器返回异常、无 prompt_id | 服务端 | 看服务器日志，换参数或重启工作流 |
| F-DUP-SUBMIT | 任务已提交过被跳过 | 流程 | 确认是否真要重跑；要重跑先清除旧任务 |
| F-LOST | 任务在远程队列消失（被取消/重启） | 服务端 | 重新提交该任务 |
| F-TIMEOUT | 生成超时疑似卡死 | 平台适配 | 换档位/降分辨率/拆段，不继续等 |

## 资产/图片层

| 失败码 | 症状 | 责任层 | 最小修复 |
|---|---|---|---|
| F-ASSET-QA | 锚点图/分镜图质检未通过 | 生图提示词/资产 | 看质检 issues，改提示词重生成 1-2 次 |
| F-ID-DRIFT | 脸/体型/发型漂移 | 资产与引用 | 只保留批准身份锚点，去掉冲突参考图 |
| F-STATE-DRIFT | 服装/伤势/携带物回退 | 资产状态版本 | 引用正确状态版本，写 must_hold |
| F-REF-SCOPE | 参考图构图/背景/光线被带入 | 引用范围 | 写明 inherit/exclude |
| F-COUNT | 人数错误/额外人物 | 空间数量 | 正向写"恰好 N 名角色各出现一次" |
| F-DUP-SUBJECT | 同一角色出现两次 | 空间数量 | 精确人数 + 反射/背景风险约束 |
| F-PROP-DUP | 道具/家具复制 | 唯一性 | 声明唯一道具及其接触关系 |

## 镜头/提示词层

| 失败码 | 症状 | 责任层 | 最小修复 |
|---|---|---|---|
| F-ACTION-OVERLOAD | 动作压缩/遗漏/顺序错 | 镜头契约 | 删除、合并或拆镜，不堆形容词 |
| F-CAMERA-CONFLICT | 多个主运镜互相争夺 | 摄影机契约 | 只保留一个主运动或拆镜 |
| F-CAMERA-PATH | 起点/路线/落点错误 | 摄影机契约 | 写完整 start/path/end |
| F-DIALOGUE-TEXT | 加词/漏词/念角色名 | 对白锁 | <d> 内只放纯对白，逐字台词+发声时间窗 |
| F-DIALOGUE-TIME | 台词太快/截断/挤掉动作 | 时长预算 | 实测朗读，延长/拆镜 |
| F-LIPSYNC | 未说话者嘴动/口型错 | 口型边界 | 只在说话者发声时间窗口型运动 |
| F-DIALOGUE-VISUALIZED | 台词内容变成闪回/额外人物 | 视觉边界 | 写"对白不视觉化 + 当前允许集合" |
| F-AUDIO-MIX | 人声不清/混响/自动音乐 | 混音边界 | 排混音优先级，明确无配乐 |
| F-AUDIO-POLLUTION | 自动音乐/旁白/字幕 | 音轨边界 | non_diegetic_music: N/A + 禁额外音轨 |

## 复测规则
- 每次只改一个主要责任层，记录 changed_variables 与 hypothesis
- 使用相同质检标准对比上一批与新批
- 连续两个批次对同一错误无改善时，停止同一路径，回到资产或镜头拆解
"""


# ---------- 规则文件加载（编辑 rules/*.md 即可改规则，无需动代码） ----------

def _load_rule(name, fallback):
    p = os.path.join(RULES_DIR, name + ".md")
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                return content
        except Exception:
            pass
    return fallback


_BUILTIN_H3_EXPAND_SYSTEM = H3_EXPAND_SYSTEM
_BUILTIN_H3_POLISH_SYSTEM = H3_POLISH_SYSTEM
_BUILTIN_SCRIPT_GEN_SYSTEM = SCRIPT_GEN_SYSTEM
_BUILTIN_SCRIPT_REWRITE_SYSTEM = SCRIPT_REWRITE_SYSTEM
_BUILTIN_SCRIPT_TEXT_SYSTEM = SCRIPT_TEXT_SYSTEM
_BUILTIN_ASSET_PROMPT_SYSTEM = ASSET_PROMPT_SYSTEM
_BUILTIN_STORY_PROMPT_SYSTEM = STORY_PROMPT_SYSTEM
_BUILTIN_FAILURE_CODES_SYSTEM = FAILURE_CODES_SYSTEM

H3_EXPAND_SYSTEM = _load_rule("h3_expand", _BUILTIN_H3_EXPAND_SYSTEM)
H3_POLISH_SYSTEM = _load_rule("h3_polish", _BUILTIN_H3_POLISH_SYSTEM)
SCRIPT_GEN_SYSTEM = _load_rule("script_gen", _BUILTIN_SCRIPT_GEN_SYSTEM)
SCRIPT_REWRITE_SYSTEM = _load_rule("script_rewrite", _BUILTIN_SCRIPT_REWRITE_SYSTEM)
SCRIPT_TEXT_SYSTEM = _load_rule("script_text", _BUILTIN_SCRIPT_TEXT_SYSTEM)
ASSET_PROMPT_SYSTEM = _load_rule("asset_prompt", _BUILTIN_ASSET_PROMPT_SYSTEM)
STORY_PROMPT_SYSTEM = _load_rule("story_prompt", _BUILTIN_STORY_PROMPT_SYSTEM)
FAILURE_CODES_SYSTEM = _load_rule("failure_codes", _BUILTIN_FAILURE_CODES_SYSTEM)


RULES = {
    "script_gen": ("SCRIPT_GEN_SYSTEM", "剧本生成（梗概 → 分镜）"),
    "script_rewrite": ("SCRIPT_REWRITE_SYSTEM", "剧本改写（结构级改编）"),
    "script_text": ("SCRIPT_TEXT_SYSTEM", "剧本片段转换（自然语言 → 分镜）"),
    "h3_expand": ("H3_EXPAND_SYSTEM", "提示词扩写（剧本 → H3 三段式）"),
    "h3_polish": ("H3_POLISH_SYSTEM", "提示词润色（对白理顺）"),
    "asset_prompt": ("ASSET_PROMPT_SYSTEM", "生图提示词修改（对话改图）"),
    "story_prompt": ("STORY_PROMPT_SYSTEM", "分镜图提示词规范（按剧情生成）"),
    "failure_codes": ("FAILURE_CODES_SYSTEM", "失败码诊断速查表（生成失败定位）"),
}


def save_rule(name, content, reset=False):
    """保存/恢复规则：写文件 + 热更新内存常量（立即生效，无需重启）。"""
    if name not in RULES:
        raise ValueError(f"未知规则：{name}")
    const_name = RULES[name][0]
    if reset:
        p = os.path.join(RULES_DIR, name + ".md")
        if os.path.exists(p):
            os.remove(p)
        globals()[const_name] = globals()["_BUILTIN_" + const_name]
        return True
    content = str(content or "").strip()
    if not content:
        raise ValueError("规则内容不能为空")
    p = os.path.join(RULES_DIR, name + ".md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    globals()[const_name] = content
    return True


def llm_text_to_script(text, token="", segments=0):
    """自然语言剧本/剧情文字 → 结构化剧本 JSON。segments>0 时强制正好 N 段。"""
    user_content = str(text)[:8000]
    if segments and segments > 0:
        user_content = (
            f"【段数硬性要求】必须正好拆成 {segments} 段（每段 5-15 秒），"
            "不许多拆、不许少拆、不许合并成 1 段以外的数量。\n\n"
            + user_content
        )
    resp = lmstudio_chat([
        {"role": "system", "content": SCRIPT_TEXT_SYSTEM},
        {"role": "user", "content": user_content},
    ], token)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    data = _extract_json_obj(content)
    if not data or not data.get("storyboard_list"):
        raise RuntimeError("AI 没有返回有效剧本 JSON")
    return data


def _extract_json_obj(out):
    """从 LLM 输出里提取 JSON 对象（容忍 markdown 包裹/思考废话/多候选块）。"""
    s = str(out or "").strip()
    m = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    starts = [i for i, ch in enumerate(s) if ch == "{"]
    for start in reversed(starts):  # 从后往前试，优先取真正的 JSON 对象
        end = s.rfind("}", start)
        if end <= start:
            continue
        raw = s[start:end + 1]
        try:
            return json.loads(raw)
        except Exception:
            pass
        # 容忍修复：去尾逗号 / 行注释 / 单引号键
        cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
        cleaned = re.sub(r"//[^\n]*", "", cleaned)
        cleaned = cleaned.replace("'", '"')
        try:
            return json.loads(cleaned)
        except Exception:
            continue
    return None


def llm_generate_script(topic, style="", segments=10, token=""):
    """剧情梗概 → AI 生成完整剧本（role_list + storyboard_list）。"""
    user = (
        f"题材/梗概：{topic or ''}\n"
        f"风格要求：{style or '怀旧温情、1980s 中国乡村校园'}\n"
        f"段数：{segments} 段左右\n"
        "请创作完整剧本 JSON。"
    )
    resp = lmstudio_chat([
        {"role": "system", "content": SCRIPT_GEN_SYSTEM},
        {"role": "user", "content": user},
    ], token)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    data = _extract_json_obj(content)
    if not data or not data.get("storyboard_list"):
        raise RuntimeError("AI 没有返回有效剧本 JSON")
    return data


def llm_rewrite_script(script, token=""):
    """剧本 → AI 改写（对白理顺/动作衔接优化）。返回 (改写后剧本, 摘要)。"""
    user = (
        "请优化下面的短剧剧本，输出与输入结构相同的 JSON。\n"
        + json.dumps(script, ensure_ascii=False, indent=1)
    )
    resp = lmstudio_chat([
        {"role": "system", "content": SCRIPT_REWRITE_SYSTEM},
        {"role": "user", "content": user},
    ], token)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    data = _extract_json_obj(content)
    if not data or not data.get("storyboard_list"):
        raise RuntimeError("AI 没有返回有效剧本 JSON")
    return data


def _extract_json_array(out):
    """从 LLM 输出里提取 JSON 数组（容忍 markdown 包裹/前后废话）。"""
    s = str(out or "").strip()
    # 剥掉 markdown 代码块
    m = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    # 找到第一组最外层方括号包裹的内容
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    raw = s[start:end + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    # 容错：去尾逗号、单引号键、注释行后重试
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    cleaned = re.sub(r"//[^\n]*", "", cleaned)
    cleaned = re.sub(r"#\s?[^\n]*", "", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _lm_token(body=None):
    """LM Studio API token：请求 body > 环境变量 > state 配置。"""
    if body and body.get("lmstudio_token"):
        return str(body["lmstudio_token"]).strip()
    env_tok = os.environ.get("LM_API_TOKEN", "").strip()
    if env_tok:
        return env_tok
    return str(load_state().get("lmstudio_token") or "").strip()


def _lm_model():
    """当前 AI 模型：state 配置 > 默认。"""
    return str(load_state().get("lmstudio_model") or LMSTUDIO_MODEL).strip() or LMSTUDIO_MODEL


def _lm_headers(token):
    h = {"User-Agent": "batch-console"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _v1(url):
    """把服务地址规范化为带 /v1 的 OpenAI 兼容前缀。
    配置里 URL 可不带 /v1（如 http://127.0.0.1:1234），已带 /v1 也能识别。"""
    url = str(url or "").rstrip("/")
    if url.endswith("/v1"):
        return url
    return url + "/v1"


def _strip_v1(url):
    """去掉 URL 尾部的 /v1（DashScope 原生 API 需要不带 /v1 的根地址）。"""
    url = str(url or "").rstrip("/")
    if url.endswith("/v1"):
        return url[:-3].rstrip("/")
    return url


def _llm_endpoints():
    """返回 (主端点, 备用端点)；端点 dict：{url, api_key, model, provider}。"""
    cfg = _CONFIG["llm"]
    ptype = str(cfg.get("provider_type") or "openai").strip() or "openai"
    local = {
        "url": str(cfg["local"]["url"] or "").rstrip("/"),
        "api_key": str(cfg["local"]["token"] or "").strip(),
        "model": str(cfg["local"]["model"] or "").strip(),
        "provider": "local",
        "provider_type": "openai",
    }
    cloud = {
        "url": str(cfg["cloud"]["base_url"] or "").rstrip("/"),
        "api_key": str(cfg["cloud"]["api_key"] or "").strip(),
        "model": str(cfg["cloud"]["model"] or "").strip(),
        "provider": "cloud",
        "provider_type": ptype,
        "enabled": bool(cfg["cloud"].get("enabled")),
    }
    if str(cfg.get("provider") or "local") == "cloud":
        return cloud, local
    return local, cloud if cloud["enabled"] and cloud["url"] else None


def _llm_openai(endpoint, messages, token="", timeout=1800):
    """OpenAI 兼容适配器：/v1/chat/completions。"""
    if not endpoint or not endpoint.get("url"):
        raise RuntimeError("语言模型端点未配置")
    api_key = token.strip() or endpoint.get("api_key") or ""
    payload = {
        "model": endpoint.get("model") or _lm_model(),
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 12000,
    }
    req = urllib.request.Request(
        _v1(endpoint["url"]) + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **_lm_headers(api_key)},
    )
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _llm_claude(endpoint, messages, token="", timeout=1800):
    """Claude（Anthropic Messages API）适配器：POST /v1/messages。"""
    if not endpoint or not endpoint.get("url"):
        raise RuntimeError("语言模型端点未配置")
    api_key = token.strip() or endpoint.get("api_key") or ""
    system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    chat = [{"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") != "system"]
    payload = {"model": endpoint.get("model") or "claude-sonnet-4", "max_tokens": 12000}
    if system:
        payload["system"] = system
    payload["messages"] = chat
    req = urllib.request.Request(
        endpoint["url"].rstrip("/") + "/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "batch-console",
        },
    )
    with _opener().open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["content"][0]["text"]
    return {"choices": [{"message": {"content": text}}]}


def _llm_dashscope(endpoint, messages, token="", timeout=1800):
    """通义千问（DashScope 原生）适配器：同步 text-generation。"""
    if not endpoint or not endpoint.get("url"):
        raise RuntimeError("语言模型端点未配置")
    api_key = token.strip() or endpoint.get("api_key") or ""
    payload = {
        "model": endpoint.get("model") or "qwen-plus",
        "input": {"messages": messages},
        "parameters": {"result_format": "message", "temperature": 0.6, "max_tokens": 12000},
    }
    req = urllib.request.Request(
        _strip_v1(endpoint["url"]) + "/api/v1/services/aigc/text-generation/generation",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with _opener().open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["output"]["choices"][0]["message"]["content"]
    return {"choices": [{"message": {"content": text}}]}


# 语言模型适配器注册表：config llm.provider_type 选择
_LLM_ADAPTERS = {
    "openai": _llm_openai,
    "claude": _llm_claude,
    "dashscope": _llm_dashscope,
}


def _chat_once(endpoint, messages, token="", timeout=1800):
    """按端点 provider_type 选择适配器调用；统一返回 OpenAI 兼容响应结构。"""
    if not endpoint or not endpoint.get("url"):
        raise RuntimeError("语言模型端点未配置")
    atype = str(endpoint.get("provider_type") or "openai").strip() or "openai"
    fn = _LLM_ADAPTERS.get(atype) or _LLM_ADAPTERS["openai"]
    return fn(endpoint, messages, token, timeout)


def check_lmstudio(token="", timeout=6):
    main, backup = _llm_endpoints()
    for ep in [main, backup]:
        if not ep or not ep.get("url"):
            continue
        try:
            api_key = token.strip() or ep.get("api_key") or ""
            req = urllib.request.Request(_v1(ep["url"]) + "/models", headers=_lm_headers(api_key))
            with _opener().open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = [m.get("id") or m.get("name") for m in data.get("data", [])]
            loaded = data.get("data") or []
            model = _lm_model()
            return {
                "ok": True,
                "provider": ep.get("provider"),
                "models": [n for n in names if n],
                "model": model,
                "available": model in [n for n in names if n],
                "loaded": [m.get("id") for m in loaded if m.get("object") == "model"],
            }
        except Exception as e:
            last_err = str(e)
    return {"ok": False, "error": last_err}


def boogu_check(timeout=4):
    try:
        req = urllib.request.Request(_v1(BOOGU_URL) + "/models", headers={"User-Agent": "batch-console"})
        with _opener().open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {"ok": True, "models": [m.get("id") for m in data.get("data", [])]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _image_gen_endpoints():
    """返回 (主端点, 备用端点)；image_gen 配置。"""
    cfg = _CONFIG["image_gen"]
    ptype = str(cfg.get("provider_type") or "openai").strip() or "openai"
    local = {"url": str(cfg["local"]["url"] or "").rstrip("/"), "provider": "local", "provider_type": "openai"}
    cloud = {
        "url": str(cfg["cloud"]["base_url"] or "").rstrip("/"),
        "api_key": str(cfg["cloud"]["api_key"] or "").strip(),
        "model": str(cfg["cloud"]["model"] or "").strip(),
        "provider": "cloud",
        "provider_type": ptype,
        "enabled": bool(cfg["cloud"].get("enabled")),
    }
    if str(cfg.get("provider") or "local") == "cloud":
        return cloud, local
    return local, cloud if cloud["enabled"] and cloud["url"] else None


def _img_openai(ep, prompt, filename, size="768x1024", timeout=300):
    """OpenAI 兼容文生图适配器（/v1/images/generations，b64 或 url 返回）。"""
    if not ep.get("url"):
        raise RuntimeError("云端文生图端点未配置")
    payload = {
        "model": ep.get("model") or "gpt-image-1",
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }
    req = urllib.request.Request(
        _v1(ep["url"]) + "/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **_lm_headers(ep.get("api_key") or "")},
    )
    with _opener().open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    b64 = data["data"][0].get("b64_json")
    if not b64:
        url = data["data"][0].get("url")
        if url:
            with _opener().open(url, timeout=timeout) as fr:
                raw = fr.read()
        else:
            raise RuntimeError("云端生图返回无图片内容")
    else:
        import base64 as _b64
        raw = _b64.b64decode(b64)
    dest = os.path.join(IMAGE_DIRS[0], filename)
    with open(dest, "wb") as f:
        f.write(raw)
    return filename, dest


def _img_dashscope(ep, prompt, filename, size="768x1024", timeout=300):
    """通义万相（DashScope）适配器：异步任务 + 轮询结果。"""
    if not ep.get("url"):
        raise RuntimeError("云端文生图端点未配置")
    api_key = ep.get("api_key") or ""
    base = ep["url"].rstrip("/")
    payload = {
        "model": ep.get("model") or "wanx2.1-t2i-turbo",
        "input": {"prompt": prompt},
        "parameters": {"size": size, "n": 1},
    }
    req = urllib.request.Request(
        _strip_v1(ep["url"]) + "/api/v1/services/aigc/multimodal-generation/generation",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-DashScope-Async": "enable",
        },
    )
    with _opener().open(req, timeout=timeout) as r:
        task = json.loads(r.read().decode("utf-8"))
    task_id = task.get("output", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"DashScope 未返回任务：{task}")
    # 轮询任务结果
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(3)
        q = urllib.request.Request(
            base + f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with _opener().open(q, timeout=timeout) as r:
            st = json.loads(r.read().decode("utf-8"))
        out = st.get("output", {})
        status = out.get("task_status")
        if status == "SUCCEEDED":
            url = out.get("results", [{}])[0].get("url")
            if not url:
                raise RuntimeError("DashScope 任务成功但无图片 URL")
            with _opener().open(url, timeout=timeout) as fr:
                raw = fr.read()
            dest = os.path.join(IMAGE_DIRS[0], filename)
            with open(dest, "wb") as f:
                f.write(raw)
            return filename, dest
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(f"DashScope 任务失败：{st}")
    raise RuntimeError("DashScope 任务超时")


_IMG_ADAPTERS = {
    "openai": _img_openai,
    "dashscope": _img_dashscope,
}


def boogu_generate(prompt, filename, size="768x1024", timeout=300, refs=None, mode=None):
    """文生图统一入口：provider=comfyui 走远程 MiniMax H3 Still，否则本地 Boogu 优先、失败降级云端。"""
    cfg = _CONFIG["image_gen"]
    if str(cfg.get("provider") or "local") == "comfyui":
        ctimeout = float((cfg.get("comfyui") or {}).get("timeout") or 900)
        return _comfyui_still_generate(prompt, filename, size, ctimeout, refs=refs, mode=mode)
    main, backup = _image_gen_endpoints()
    last_err = None
    if main.get("provider") == "cloud":
        try:
            atype = str(main.get("provider_type") or "openai").strip() or "openai"
            return _IMG_ADAPTERS.get(atype, _img_openai)(main, prompt, filename, size, timeout)
        except Exception as e:
            last_err = e
            if backup is None or backup.get("provider") != "local":
                raise
    # 本地 Boogu
    try:
        return _boogu_local(prompt, filename, size, timeout)
    except Exception as e:
        last_err = e
        if backup and backup.get("provider") == "cloud":
            atype = str(backup.get("provider_type") or "openai").strip() or "openai"
            return _IMG_ADAPTERS.get(atype, _img_openai)(backup, prompt, filename, size, timeout)
        raise


def _boogu_local(prompt, filename, size="768x1024", timeout=300):
    """调本地 Boogu-Image 生成图片并保存到 素材/ 目录。返回 (filename, 绝对路径)。"""
    # 每次调用带随机 seed：重试/重新生成会得到不同画面（Boogu 未传 seed 时曾固定 42 出同一张图）
    payload = {"model": "boogu-image", "prompt": prompt, "size": size, "seed": random.randrange(2 ** 31)}
    req = urllib.request.Request(
        BOOGU_URL + "/v1/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "batch-console"},
    )
    with _opener().open(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    b64 = d.get("data", [{}])[0].get("b64_json")
    if not b64:
        raise RuntimeError("Boogu 未返回图片数据")
    raw = base64.b64decode(b64)
    if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        filename += ".png"
    dest = os.path.join(IMAGE_DIRS[0], filename)
    with open(dest, "wb") as f:
        f.write(raw)
    return filename, dest


def _parse_img_size(size):
    """'768x1024' / '768*1024' → (宽, 高)；H3 Still 画布必须是 32 的倍数。"""
    m = re.fullmatch(r"\s*(\d+)\s*[xX*]\s*(\d+)\s*", str(size or "").strip())
    if not m:
        raise ValueError(f"非法图片尺寸：{size}（应为 宽x高）")
    w, h = int(m.group(1)), int(m.group(2))
    if w % 32 or h % 32:
        raise ValueError(f"H3 Still 画布宽高必须是 32 的倍数：{size}")
    return w, h


def _write_solid_png(path, width, height, rgb=(128, 128, 128)):
    """纯标准库写纯色 PNG（H3 Still 纯文生图需要 edit_image 占位图）。"""
    import struct
    import zlib

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as f:
        f.write(png)
    return path


def _comfyui_still_generate(prompt, filename, size="768x1024", timeout=900, refs=None, mode=None):
    """远程 ComfyUI MiniMax H3 Still 文生图（官方 T8 节点）。

    mode=t2i     : edit_image 用纯灰占位图，提示词纯文生图（场景图等无参考场景用）；
    mode=refedit : edit_image=场景图（refs[0]），ref_images=角色锚点（refs[1:]），
                   画面人数/形象被参考图锁死，解决"多画第三人/多余肢体"问题。
    """
    cfg = _CONFIG.get("image_gen", {}).get("comfyui") or {}
    server = str(cfg.get("server") or DEFAULT_SERVER).rstrip("/")
    unet = str(cfg.get("unet") or "minimax_h3_ref2va_pruned_int8_convrot.safetensors")
    clip_name = str(cfg.get("clip") or "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    vae = str(cfg.get("vae") or "minimax_h3_video_vae_fp16.safetensors")
    steps = int(cfg.get("steps") or 20)
    target_mode = str(cfg.get("target_mode") or "micro_video_5_frames")
    width, height = _parse_img_size(size or cfg.get("size") or "768x1024")
    refs = [str(x) for x in (refs or []) if str(x).strip()]
    mode = str(mode or "").strip() or ("refedit" if refs else "t2i")
    if mode == "refedit" and not refs:
        mode = "t2i"
    ref_imgs = refs[1:] if mode == "refedit" else []
    if mode == "refedit":
        ref_strength = float(cfg.get("reference_strength") or 0.75)
        prompt = str(prompt or "") + " 环境与空间布局以 <Picture 1> 为基准。"
        if ref_imgs:
            role_tags = "、".join(f"<Picture {i + 2}>" for i in range(len(ref_imgs)))
            prompt += (
                f" 画面中只允许出现 {role_tags} 中的 {len(ref_imgs)} 个人，"
                " 每个人的面容、发型、服装必须与对应参考图完全一致；"
                " 绝对禁止出现参考图之外的任何第三人、多余肢体、多余手指、倒影或背景人物。"
            )
    else:
        ref_strength = float(cfg.get("t2i_reference_strength") or 0.2)

    # 1) 准备 edit_image：refedit 用场景图，t2i 用纯灰占位图；上传到服务器 input 目录
    if mode == "refedit":
        edit_local = find_image(refs[0])
        if not edit_local:
            raise RuntimeError(f"找不到场景参考图：{refs[0]}")
        edit_fn = os.path.basename(refs[0])
    else:
        edit_fn = f"_h3_still_placeholder_{width}x{height}.png"
        edit_local = os.path.join(tempfile.gettempdir(), edit_fn)
        if not os.path.isfile(edit_local):
            _write_solid_png(edit_local, width, height)
    upload_image(server, edit_local, edit_fn)

    # 2) 角色锚点参考图（ref_images，Picture 2+）
    for r in ref_imgs:
        lp = find_image(r)
        if not lp:
            raise RuntimeError(f"找不到角色参考图：{r}")
        upload_image(server, lp, os.path.basename(r))

    # 3) 构建 H3 Still API 图（官方 T8 节点结构，见 T8mars 官方示例）
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": edit_fn}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": "minimax", "device": "default"}},
        "4": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
    }
    cond_inputs = {
        "clip": ["3", 0],
        "video_vae": ["2", 0],
        "prompt": prompt,
        "canvas_mode": "custom",
        "width": width,
        "height": height,
        "target_mode": target_mode,
        "reference_strength": ref_strength,
        "audio_target": "generate_and_discard",
        "strict_prompt_tags": True,
        "ref_image_size": "match",
        "edit_image": ["1", 0],
    }
    nid = 5
    if ref_imgs:
        for i, r in enumerate(ref_imgs, 1):
            graph[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": os.path.basename(r)}}
            # V3 节点 autogrow 输入用打平点号键（ref_images.ref_image_N）
            cond_inputs[f"ref_images.ref_image_{i}"] = [str(nid), 0]
            nid += 1
    graph[str(nid)] = {
        "class_type": "MiniMaxH3StillConditioningT8",
        "inputs": cond_inputs,
    }
    cond_id = str(nid)
    nid += 1
    graph[str(nid)] = {
        "class_type": "MiniMaxH3DualClockSamplerT8",
        "inputs": {
            "model": ["4", 0],
            "av_latent": [cond_id, 1],
            "steps": steps,
            "shift_video": 12.0,
            "shift_audio": 3.0,
        },
    }
    sampler_id = str(nid)
    nid += 1
    graph[str(nid)] = {"class_type": "RandomNoise", "inputs": {"noise_seed": random.randrange(2 ** 31)}}
    noise_id = str(nid)
    nid += 1
    graph[str(nid)] = {"class_type": "BasicGuider", "inputs": {"model": [sampler_id, 0], "conditioning": [cond_id, 0]}}
    guider_id = str(nid)
    nid += 1
    graph[str(nid)] = {
        "class_type": "SamplerCustomAdvanced",
        "inputs": {
            "noise": [noise_id, 0],
            "guider": [guider_id, 0],
            "sampler": [sampler_id, 1],
            "sigmas": [sampler_id, 2],
            "latent_image": [cond_id, 1],
        },
    }
    sample_id = str(nid)
    nid += 1
    graph[str(nid)] = {
        "class_type": "MiniMaxH3StillDecodeT8",
        "inputs": {"av_latent": [sample_id, 0], "video_vae": ["2", 0], "frame_selection": "middle", "frame_index": 0},
    }
    decode_id = str(nid)
    nid += 1
    graph[str(nid)] = {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "H3_Still_Storyboard", "images": [decode_id, 0]},
    }

    # 4) 提交 + 轮询 /history 取结果
    try:
        resp = api_post(server, "/prompt", {"prompt": graph, "client_id": "batch_console"}, timeout=60)
    except Exception as e:
        raise RuntimeError(f"H3 Still 提交失败：{e}")
    if resp.get("node_errors"):
        detail = json.dumps(resp["node_errors"], ensure_ascii=False)[:500]
        raise RuntimeError(f"H3 Still 节点校验失败：{detail}")
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"H3 Still 提交异常：{resp}")
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        time.sleep(3)
        try:
            hist = api_get(server, f"/history/{pid}", timeout=15)
        except Exception:
            continue
        entry = (hist or {}).get(pid)
        if not entry:
            continue
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            msgs = json.dumps(status.get("messages") or [], ensure_ascii=False)[:300]
            raise RuntimeError(f"H3 Still 生成失败：{msgs}")
        for _node_id, out in (entry.get("outputs") or {}).items():
            for im in out.get("images", []):
                q = urllib.parse.urlencode({
                    "filename": im.get("filename", ""),
                    "subfolder": im.get("subfolder", ""),
                    "type": im.get("type", "output"),
                })
                req = urllib.request.Request(server + "/view?" + q, headers={"User-Agent": "batch-console"})
                with _opener().open(req, timeout=600) as r:
                    raw = r.read()
                if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    filename += ".png"
                dest = os.path.join(IMAGE_DIRS[0], filename)
                with open(dest, "wb") as f:
                    f.write(raw)
                return filename, dest
    raise RuntimeError(f"H3 Still 生成超时（{timeout}s）；远程队列可能丢失，请检查 ComfyUI 状态")


def _still_cfg():
    """image_gen.comfyui 配置（do_POST 内不能直接用 _CONFIG，会撞 global 声明）。"""
    return _CONFIG.get("image_gen", {}).get("comfyui") or {}


# ---------- 图片自动质检（本地视觉服务 8001） ----------

_VISION_ENV = None


def _vision_config():
    """视觉质检配置：config.json 优先，兼容旧版 ~/.codex/vision/.env。"""
    global _VISION_ENV
    if _VISION_ENV is None:
        cfg = _CONFIG.get("vision") or {}
        _VISION_ENV = {
            "DASHSCOPE_BASE_URL": str(cfg.get("base_url") or "").rstrip("/"),
            "VISION_MODEL": str(cfg.get("model") or ""),
            "DASHSCOPE_API_KEY": str(cfg.get("api_key") or ""),
        }
        # 兼容旧版：.env 里未在 config 中填写的项兜底
        p = os.path.expanduser("~/.codex/vision/.env")
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    key = k.strip()
                    if not _VISION_ENV.get("DASHSCOPE_BASE_URL") and key == "DASHSCOPE_BASE_URL":
                        _VISION_ENV[key] = v.strip().strip('"').strip("'")
                    if not _VISION_ENV.get("VISION_MODEL") and key == "VISION_MODEL":
                        _VISION_ENV[key] = v.strip().strip('"').strip("'")
                    if not _VISION_ENV.get("DASHSCOPE_API_KEY") and key == "DASHSCOPE_API_KEY":
                        _VISION_ENV[key] = v.strip().strip('"').strip("'")
    return _VISION_ENV


def vision_ask(image_path, prompt, timeout=120):
    """调本地视觉服务（OpenAI 兼容，图片 base64）→ 返回文本。"""
    cfg = _vision_config()
    base = cfg.get("DASHSCOPE_BASE_URL", "http://127.0.0.1:8001/v1")
    model = cfg.get("VISION_MODEL", "Qwen3.6-35B-A3B-4bit")
    key = cfg.get("DASHSCOPE_API_KEY", "")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(ext, "image/jpeg")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "stream": False, "max_tokens": 1024,
    }
    # 本地 oMLX：关闭思考模式，让质检模型直接输出 JSON（实测 enable_thinking=false 输出干净稳定）
    if "127.0.0.1" in base or "localhost" in base:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with _opener().open(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d.get("choices", [{}])[0].get("message", {}).get("content", "")


def verify_asset(image_path, kind, expected=None, timeout=120):
    """质检图片：角色（性别/服装）、场景（无人/无现代物品）、分镜（形象/穿帮）。"""
    expected = expected or {}
    if kind == "role":
        gender = expected.get("gender") or "未知"
        look = expected.get("look") or ""
        hair = expected.get("hair") or ""
        costume = expected.get("costume") or ""
        check_look = "；".join(x for x in [hair, costume] if x) or look
        view = expected.get("view")
        if view == "side":
            q = (f"你是图片质检员。检查这张角色侧面参考图（90度正侧面），严格只输出 JSON："
                 f"{{\"ok\": true或false, \"issues\": [\"问题1\", ...]}}\n"
                 f"检查项：1. 人物侧面轮廓、发型、服装必须与设定一致：{check_look} "
                 f"2. 侧面视角正常（人物侧身站立，面部朝向画面侧面，不要求正脸） "
                 f"3. 画面干净：无多余肢体/多余手指、无重复人物、无文字乱码、无现代物品。\n"
                 "ok=true 表示全部通过；否则 ok=false 并列出具体问题。")
        elif view == "back":
            q = (f"你是图片质检员。检查这张角色背面参考图，严格只输出 JSON："
                 f"{{\"ok\": true或false, \"issues\": [\"问题1\", ...]}}\n"
                 f"检查项：1. 背面视角正常（人物背对镜头，看不到正脸属正常） "
                 f"2. 发型与服装必须与设定一致：{check_look} "
                 f"3. 画面干净：无多余肢体/多余手指、无重复人物、无文字乱码、无现代物品。\n"
                 "ok=true 表示全部通过；否则 ok=false 并列出具体问题。")
        elif view == "face":
            q = (f"你是图片质检员。检查这张角色脸部特写参考图，严格只输出 JSON："
                 f"{{\"ok\": true或false, \"issues\": [\"问题1\", ...]}}\n"
                 f"检查项：1. 五官清晰、面部占画面主体 2. 发型与设定一致：{hair or check_look} "
                 f"3. 人物性别必须是「{gender}」 4. 画面干净：无多余肢体/手指、无重复人物、无文字乱码。\n"
                 "ok=true 表示全部通过；否则 ok=false 并列出具体问题。")
        else:
            q = (f"你是图片质检员。检查这张角色锚点图，严格只输出 JSON：{{\"ok\": true或false, \"issues\": [\"问题1\", ...]}}\n"
                 f"检查项：1. 人物性别必须是「{gender}」 2. 发型服装必须与设定一字不差一致：{check_look} "
                 f"3. 画面干净：无多余肢体/多余手指、无重复人物、无文字乱码、无现代物品 4. 面部清晰五官正常。\n"
                 f"若发型或服装与设定不符，必须列为问题（如'服装与设定不符：应为碎花连衣裙，实际是…'）。\n"
                 "ok=true 表示全部通过；否则 ok=false 并列出具体问题。")
    elif kind == "scene":
        desc = expected.get("desc") or ""
        desc_line = f"1. 场景必须与描述一致：{desc}" if desc else "1. 场景符合「1980年代中国乡村环境」"
        q = ("你是图片质检员。检查这张场景参考图，严格只输出 JSON：{\"ok\": true或false, \"issues\": [...]}\n"
             f"检查项：{desc_line} 2. 画面内无人物 "
             "3. 无文字乱码 4. 无现代物品（路灯/汽车/塑料/现代包装等）。\n"
             "若场景与描述明显不符，必须列为问题。\n"
             "ok=true 表示全部通过；否则 ok=false 并列出具体问题。")
    else:
        look = expected.get("look") or ""
        people = expected.get("people_count")
        people_line = (
            f" 0. 画面里必须严格只有 {people} 个人（计数铁律：背景人物、画面边缘半身人、"
            "被遮挡的人、镜像/倒影人物都算人，必须全部计入，总数必须恰好为 "
            f"{people}，多一个即不通过）；如果无法明确清点人数，ok 必须为 false；"
            "绝对没有第三人、没有多余的肢体/手臂/手伸入画面"
            if people else ""
        )
        q = (f"你是图片质检员。检查这张分镜图，严格只输出 JSON：{{\"ok\": true或false, \"issues\": [...]}}\n"
             f"检查项：{people_line} 1. 人物形象：{look} 2. 肢体完整：无多余手指/手臂/腿部异常纹理 "
             "3. 画面干净：无文字乱码、无重复人物、无现代物品、道具正常（无塑料包装盒/饮料瓶）。\n"
             "ok=true 表示全部通过；否则 ok=false 并列出具体问题。")
    try:
        content = vision_ask(image_path, q, timeout)
    except Exception as e:
        # 质检服务不可用 → 无法确认 → 判不通过（fail-closed，避免坏图被静默放行）
        return {"ok": False, "issues": [f"质检服务不可用：{e}"], "error": str(e)}
    data = _extract_json_obj(content) or {}
    if not data:
        # 模型没吐出标准 JSON → 无法确认 → 判不通过
        return {"ok": False, "issues": ["质检模型未返回可解析结果，判定不通过"], "error": "unparsable"}
    ok_raw = data.get("ok")
    if isinstance(ok_raw, str):
        ok = ok_raw.strip().lower() in ("true", "1", "yes", "通过")
    else:
        ok = bool(ok_raw)
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [issues]
    return {"ok": ok, "issues": [str(x) for x in issues][:5]}


_ASSET_TAG = {"role": "锚点", "scene": "场景", "story": "分镜"}


def _gen_asset_file(kind, key, prompt, expected=None, view=None, refs=None, mode=None):
    """生图 + 质检（最多 3 次，每次随机 seed 出新图）。返回 (filename, verified, issues, attempts)。"""
    expected = dict(expected or {})
    if view:
        expected["view"] = view
    tag = _ASSET_TAG.get(kind, kind)
    safe = re.sub(r'[\\/:*?"<>|]', '', str(key))[:20] or "x"
    last_fn, last_issues, attempts = None, [], 0
    # 分镜图抽卡概率约 1/4（双人场景 Boogu 容易多画人），重试 8 次大幅提高出对概率
    max_tries = 8 if kind == "story" else 3
    for _ in range(max_tries):
        filename = f"{tag}_{safe}_{int(time.time() * 1000):x}_{random.randrange(16 ** 4):04x}.png"
        fn, dest = boogu_generate(prompt, filename, "768x1024", refs=refs, mode=mode)
        attempts += 1
        v = verify_asset(dest, kind, expected)
        last_fn, last_issues = fn, v.get("issues", [])
        if v.get("ok"):
            return fn, True, [], attempts
        time.sleep(1)
    # 分镜图人数是硬性指标：8 次质检都不过就不再入库，交给上层报错
    if kind == "story" and not v.get("ok"):
        raise ValueError(f"质检未通过（{max_tries} 次）：{'；'.join(last_issues)[:200] or '人数/穿帮异常'}")
    return last_fn, False, last_issues, attempts


def _register_asset(kind, key, filename, view=None, meta=None):
    """把生成结果登记进项目状态并立即保存（角色/场景/分镜/四视图）。"""
    st = load_state()
    proj = dict(st.get("project") or {})
    ai = dict(proj.get("asset_imgs") or {})
    am = dict(proj.get("asset_meta") or {})
    ast = dict(proj.get("asset_state") or {})
    if kind == "role":
        ai.setdefault("role", {})[str(key)] = filename
        ai.setdefault("roleViews", {}).setdefault(str(key), {})[view or "front"] = filename
        r = ast.setdefault("roles", {}).setdefault(str(key), {})
        r.setdefault("views", {})[view or "front"] = filename
        if not view or view == "front":
            r["reference"] = filename
    elif kind == "scene":
        ai.setdefault("scene", {})[str(key)] = filename
        ast.setdefault("scenes", {}).setdefault(str(key), {})["reference"] = filename
    else:
        ai.setdefault("story", {})[str(key)] = filename
    am.setdefault(kind, {})[str(key)] = meta or {"verified": False, "issues": [], "attempts": 1}
    proj["asset_imgs"] = ai
    proj["asset_meta"] = am
    proj["asset_state"] = ast
    st["project"] = proj
    projects = st.get("projects") or {}
    if proj.get("name"):
        snap = dict(proj)
        snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        projects[proj["name"]] = snap
        st["projects"] = projects
    save_state(st)


def _story_refs(proj, story_key):
    """分镜图参考图：场景图（Picture 1）+ 出场角色正面锚点（Picture 2+）。"""
    raw_key = str(story_key or "").strip()
    m = re.fullmatch(r"[sS](\d+)", raw_key)
    if m:
        idx = int(m.group(1)) - 1  # 前端单张生成用 s2/s3（1 基）
    else:
        try:
            idx = int(raw_key)
        except (TypeError, ValueError):
            return []
    sb = ((proj.get("current_script") or {}).get("storyboard_list")) or []
    seg = sb[idx] if 0 <= idx < len(sb) else {}
    if not seg:
        return []
    ai = proj.get("asset_imgs") or {}
    scene_imgs = ai.get("scene") or {}
    role_imgs = ai.get("role") or {}
    role_views = ai.get("roleViews") or {}
    refs = []
    scene_name = str(_pick(seg, "scene", "location", "place", "scene_name") or "").strip()
    if scene_name and scene_imgs.get(scene_name):
        refs.append(scene_imgs[scene_name])
    roles = seg.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    for rn in roles:
        rn = str(rn).strip()
        if not rn:
            continue
        img = (role_views.get(rn) or {}).get("front") or role_imgs.get(rn)
        if img:
            refs.append(img)
    return refs


def start_assets_batch_job(items):
    """服务端后台批量生图任务（每张立即保存，前端轮询进度，刷新页面可恢复）。"""
    tid = "assets_" + uuid.uuid4().hex[:10]
    job = {"status": "queued", "done": 0, "total": len(items), "current": "", "error": None, "failed": []}
    with ASSET_JOBS_LOCK:
        ASSET_JOBS[tid] = job
    st = load_state()
    st["assets_gen_task"] = {"task_id": tid, "status": "queued"}
    save_state(st)

    def work():
        job["status"] = "running"
        try:
            st0 = load_state()
            proj = dict(st0.get("project") or {})
            still_cfg = _CONFIG.get("image_gen", {}).get("comfyui") or {}
            story_mode = str(still_cfg.get("story_mode") or "refedit")
            for i, item in enumerate(items):
                kind = item.get("kind") or "story"
                key = item.get("key")
                prompt = str(item.get("prompt") or "").strip()
                view = item.get("view")
                expected = item.get("expected") or {}
                label = item.get("label") or f"{kind} {key}"
                job["current"] = label
                try:
                    refs = item.get("refs")
                    mode = item.get("mode")
                    if kind == "story":
                        if not refs:
                            refs = _story_refs(proj, key)
                        if not mode:
                            mode = story_mode
                    fn, ok, issues, attempts = _gen_asset_file(kind, key, prompt, expected, view, refs, mode)
                    _register_asset(kind, key, fn, view, {"verified": ok, "issues": issues, "attempts": attempts})
                except Exception as e:
                    print(f"[assets] {label} 失败：{e}", flush=True)
                    job["error"] = f"{label} 失败：{e}"
                    job["failed"].append({"label": label, "error": str(e)[:160]})
                job["done"] = i + 1
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
        finally:
            job["current"] = ""
            st = load_state()
            st.pop("assets_gen_task", None)
            save_state(st)

    threading.Thread(target=work, daemon=True).start()
    return tid


def llm_expand_storyboards(data, token=""):
    """调用本地 LM Studio（Qwen3.6）把分镜批量扩写成 H3 提示词。

    返回 {分镜序号: prompt}。失败/超时抛异常，由调用方回退规则扩写。
    """
    role_map = _extract_roles(data)
    storyboards = _extract_storyboards(data)
    items = []
    for i, sb in enumerate(storyboards):
        item = {
            "id": i + 1,
            "scene": str(_pick(sb, "scene", "location", "place", "scene_name") or "").strip(),
            "roles": _sb_roles(sb),
            "role_desc": {rn: (role_map[rn].get("role_desc") if rn in role_map else "")
                          for rn in _sb_roles(sb)},
            "action": str(_pick(sb, "action", "motion", "action_desc") or "").strip(),
            "dialogue": str(_pick(sb, "dialogue", "line", "speech", "台词") or "").strip(),
            "camera": str(_pick(sb, "camera", "camera_move", "shot_type") or "").strip(),
            "duration_s": _sb_duration(sb),
            "emotion": str(_pick(sb, "emotion", "mood", "tone") or "").strip(),
        }
        items.append(item)
    asset_block = _asset_state_block()
    user_content = (
        f"剧本标题：{data.get('title') or data.get('name') or '未命名'}\n"
        + (asset_block + "\n" if asset_block else "")
        + "以下每个分镜扩写成完整 H3 三段式提示词。\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )
    resp = lmstudio_chat([
        {"role": "system", "content": H3_EXPAND_SYSTEM},
        {"role": "user", "content": user_content},
    ], token)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    arr = _extract_json_array(content)
    if not arr:
        raise RuntimeError("LLM 返回内容无法解析为 JSON 数组")
    prompts = {}
    for x in arr:
        try:
            prompts[int(x.get("id"))] = str(x.get("prompt", "")).strip()
        except (TypeError, ValueError, AttributeError):
            continue
    if not prompts:
        raise RuntimeError("LLM 返回 JSON 里没有可用的 prompt")
    return prompts


def _storyboard_item(sb, role_map):
    return {
        "id": 1,
        "scene": str(_pick(sb, "scene", "location", "place", "scene_name") or "").strip(),
        "roles": _sb_roles(sb),
        "role_desc": {rn: (role_map[rn].get("role_desc") if rn in role_map else "")
                      for rn in _sb_roles(sb)},
        "action": str(_pick(sb, "action", "motion", "action_desc") or "").strip(),
        "dialogue": str(_pick(sb, "dialogue", "line", "speech", "台词") or "").strip(),
        "camera": str(_pick(sb, "camera", "camera_move", "shot_type") or "").strip(),
        "duration_s": _sb_duration(sb),
        "emotion": str(_pick(sb, "emotion", "mood", "tone") or "").strip(),
    }


def _asset_state_block():
    """从当前项目资产状态表取事实，供扩写/生图引用（身份与状态分开，一字不差）。"""
    st = load_state()
    proj = st.get("project") or {}
    ast = proj.get("asset_state") or {}
    roles = ast.get("roles") or {}
    scenes = ast.get("scenes") or {}
    lines = []
    for name, r in roles.items():
        identity = str(r.get("identity") or "").strip()
        costume = str(r.get("costume") or "").strip()
        hair = str(r.get("hair") or "").strip()
        ref = str(r.get("reference") or "").strip()
        views = r.get("views") or {}
        bits = []
        if identity:
            bits.append(f"身份：{identity}")
        if hair:
            bits.append(f"发型：{hair}")
        if costume:
            bits.append(f"服装：{costume}")
        if ref:
            bits.append(
                f"参考图：{os.path.basename(ref)}（只继承身份/发型/服装，"
                "排除原图姿势/构图/背景/光线/调色）"
            )
        view_names = []
        for v in ("front", "face", "side", "back"):
            if views.get(v):
                view_names.append({"front": "正面", "face": "脸部特写", "side": "侧面", "back": "背面"}[v])
        if view_names:
            bits.append(
                f"多视图：{'、'.join(view_names)}（{len(view_names)} 个视角均为同一人物，"
                "锁定同一身份/发型/服装）"
            )
        if bits:
            lines.append(f"- {name}：{'；'.join(bits)}")
    for name, s in scenes.items():
        desc = str(s.get("desc") or "").strip()
        ref = str(s.get("reference") or "").strip()
        bits = []
        if desc:
            bits.append(f"场景：{desc}")
        if ref:
            bits.append(
                f"参考图：{os.path.basename(ref)}（只继承空间布局/陈设/氛围，"
                "排除原图构图/机位/光线/景深）"
            )
        if bits:
            lines.append(f"- {name}：{'；'.join(bits)}")
    if not lines:
        return ""
    return (
        "【角色/场景资产状态：必须一字不差引用，不得自行改写服装、发型与身份】\n"
        + "\n".join(lines)
    )


_VIEW_ZH = {"front": "正面", "face": "脸部特写", "side": "侧面", "back": "背面"}


def _split_three_fields(prompt):
    """拆三段式 → (body, sound, music)。"""
    p = str(prompt or "").strip()
    m_desc = re.search(
        r"(?:integrated_multimodal_description|detailed_description):\s*(.+?)(?=\n?\s*overall_soundscape:|\n?\s*non_diegetic_music:|\Z)",
        p, re.S,
    )
    m_snd = re.search(r"overall_soundscape:\s*(.+?)(?=\n?\s*non_diegetic_music:|\Z)", p, re.S)
    m_mus = re.search(r"non_diegetic_music:\s*(.+?)(?=\Z)", p, re.S)
    return (
        m_desc.group(1).strip() if m_desc else "",
        m_snd.group(1).strip() if m_snd else "",
        m_mus.group(1).strip() if m_mus else "",
    )


def to_ref2va_six_section(prompt, task=None):
    """三段式 → 官方 Ref2VA 六段式（中文正文 + 英文字段名/标签）。

    图片编号 = 提交时 task['images'] 的顺序（前端已按 角色多视图 → 场景图 → 分镜图 排好）。
    subject_definitions 把角色/场景映射到 <Subject N> 与 <Picture N>，分镜图声明为
    故事板参考（不锁定首帧），链帧（如出现在 R2V 中）声明为 keyframe completion。
    """
    task = task or {}
    p = str(prompt or "").strip()
    if not p:
        return p
    if "subject_definitions:" in p:
        return p  # 已是六段式
    body, sound, music = _split_three_fields(p)
    if not body:
        return p
    st = load_state()
    proj = st.get("project") or {}
    ast = proj.get("asset_state") or {}
    role_state = ast.get("roles") or {}
    scene_state = ast.get("scenes") or {}
    ai = proj.get("asset_imgs") or {}
    role_imgs = ai.get("role") or {}
    scene_imgs = ai.get("scene") or {}
    role_desc_map = {
        (r.get("role_name") or r.get("name") or ""): str(r.get("role_desc") or "")
        for r in ((proj.get("current_script") or {}).get("role_list") or [])
    }
    images = [x for x in (task.get("images") or []) if x]
    pic_of = {img: i + 1 for i, img in enumerate(images)}
    seg_roles = [str(x) for x in (task.get("roles") or []) if x]
    seg_scene = str(task.get("scene") or "").strip()
    story_img = str(task.get("story_image") or "").strip()

    # 每个角色的参考图（reference 兜底 + 多视图）
    role_refs = {}
    for name in list(role_state.keys()) + [k for k in role_imgs.keys() if k not in role_state]:
        s = role_state.get(name) or {}
        refs = []
        if s.get("reference"):
            refs.append(str(s["reference"]))
        if role_imgs.get(name):
            refs.append(str(role_imgs[name]))  # 权威：当前实际角色锚点图
        for v in ("front", "face", "side", "back"):
            vp = (s.get("views") or {}).get(v)
            if vp:
                refs.append(str(vp))
        role_refs[name] = list(dict.fromkeys(x for x in refs if x))

    subject_lines = []
    pic_lines = []
    retention_lines = []
    subject_of = {}   # role name -> <Subject N>
    scene_subject = None
    n = 0

    for name in seg_roles:
        refs = role_refs.get(name) or []
        pics = [pic_of[r] for r in refs if r in pic_of]
        if not pics:
            continue
        n += 1
        subject_of[name] = n
        s = role_state.get(name) or {}
        desc_bits = []
        if s.get("identity"):
            desc_bits.append(f"身份：{s['identity']}")
        if s.get("hair"):
            desc_bits.append(f"发型：{s['hair']}")
        if s.get("costume"):
            desc_bits.append(f"服装：{s['costume']}")
        if not desc_bits and role_desc_map.get(name):
            desc_bits.append(f"设定：{role_desc_map[name]}")
        views = []
        for v in ("front", "face", "side", "back"):
            vp = (s.get("views") or {}).get(v)
            if vp and vp in pic_of:
                views.append(_VIEW_ZH[v])
        view_txt = ("，" + "、".join(views) + " 多视角") if views else ""
        pic_txt = "、".join(f"<Picture {x}>" for x in pics)
        subject_lines.append(
            f"<Subject {n}> 是角色「{name}」（{'；'.join(desc_bits) or '身份未填'}），"
            f"其外貌来自 {pic_txt}{view_txt}。"
            "参考图只继承人物身份、发型、服装，排除原图姿势、构图、背景、光线与调色。"
        )
        retention_lines.append(
            f"<Subject {n}> (appears in [Shot 1]): fully_preserved - "
            f"「{name}」的身份、发型、服装完整保留，仅按剧情做动作与表情变化。"
        )

    if seg_scene and scene_state.get(seg_scene):
        ref = str(scene_state[seg_scene].get("reference") or scene_imgs.get(seg_scene) or "").strip()
        if ref in pic_of:
            n += 1
            scene_subject = n
            s = scene_state[seg_scene]
            desc = str(s.get("desc") or "").strip() or "空间布局与氛围"
            subject_lines.append(
                f"<Subject {n}> 是「{seg_scene}」环境（{desc}），来自 <Picture {pic_of[ref]}>。"
                "只继承空间布局、陈设与氛围，排除原图构图、机位与光线。"
            )
            retention_lines.append(
                f"<Subject {n}> (appears in [Shot 1]): fully_preserved - "
                f"「{seg_scene}」环境的空间布局与氛围完整保留。"
            )

    if story_img and story_img in pic_of:
        pic_lines.append(
            f"<Picture {pic_of[story_img]}> 是本段故事板参考图，定义机位、景别、构图与人物站位，"
            "不作为首帧锁定。"
        )
        retention_lines.append(
            f"<Picture {pic_of[story_img]}> (storyboard): weak_reference - "
            "仅参考其构图、机位与人物站位，不复制其静态画面。"
        )

    for ci in images:
        if str(ci).startswith("chain_") and ci in pic_of:
            pic_lines.append(
                f"<Picture {pic_of[ci]}> 是上一段末帧，作为本视频第 0.00 秒的首帧"
                "（keyframe completion），本段从该帧的场景、人物位置与光线无缝延续。"
            )
            retention_lines.append(
                f"<Picture {pic_of[ci]}> (first frame): fully_preserved - "
                "作为本段首帧完整保留其场景、人物位置与光线。"
            )

    if not subject_lines and not pic_lines:
        return p  # 没有可定义的参考，保持三段式

    # summary
    task_types = ["reference generation"]
    if any(str(x).startswith("chain_") and x in pic_of for x in images):
        task_types.append("keyframe completion")
    subjects_txt = "、".join(
        f"<Subject {subject_of[nm]}>（{nm}）" for nm in seg_roles if nm in subject_of
    )
    if scene_subject:
        subjects_txt += (("、" if subjects_txt else "") + f"<Subject {scene_subject}>（{seg_scene}环境）")
    action_txt = str(task.get("name") or "本段剧情")
    summary = (
        f"[{' + '.join(task_types)}] 本段（{action_txt}）展示 "
        f"{subjects_txt or '主体内容'}；人物身份与场景布局分别由对应参考图锁定，"
        "构图按故事板参考图规划，声音与画面同步生成。"
    )

    # detailed_description：首次出现角色名时插入 <Subject N> 标签（正文保持中文可读）
    marked = body
    for name, sid in subject_of.items():
        idx = marked.find(name)
        if idx >= 0:
            marked = marked[:idx] + f"<Subject {sid}>（{name}）" + marked[idx + len(name):]

    sections = ["subject_definitions:\n" + "\n".join(subject_lines + pic_lines)]
    sections.append("summary:\n" + summary)
    sections.append("retention_analysis:\n" + "\n".join(retention_lines))
    sections.append("detailed_description:\n" + marked)
    if sound:
        sections.append("overall_soundscape:\n" + sound)
    if music:
        sections.append("non_diegetic_music:\n" + music)
    return "\n\n".join(sections)


def llm_expand_one(sb, role_map, token="", prev_prompt=""):
    """单段扩写 → 返回 H3 prompt。"""
    item = _storyboard_item(sb, role_map)
    asset_block = _asset_state_block()
    parts = []
    if asset_block:
        parts.append(asset_block)
    if prev_prompt:
        parts.append(
            "上一段提示词（本段必须承接它的末帧状态：人物位置/手部/视线/服装，"
            "并在本段结尾写明尾帧 close_state 供下一段衔接）：\n" + str(prev_prompt)[:2500]
        )
    parts.append("扩写下面这 1 个分镜为完整 H3 三段式提示词：\n" + json.dumps([item], ensure_ascii=False, indent=1))
    user_content = "\n\n".join(parts)
    resp = lmstudio_chat([
        {"role": "system", "content": H3_EXPAND_SYSTEM},
        {"role": "user", "content": user_content},
    ], token)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    arr = _extract_json_array(content)
    if not arr:
        raise RuntimeError("LLM 返回内容无法解析")
    return str(arr[0].get("prompt", "")).strip()


def start_expand_job(text, token=""):
    """后台逐段扩写剧本 → 返回任务 id（前端轮询进度）。"""
    tid = "expand_" + uuid.uuid4().hex[:10]
    job = {
        "status": "queued", "done": 0, "total": 0, "current": "",
        "error": None, "tasks": None, "meta": {},
    }
    with EXPAND_JOBS_LOCK:
        EXPAND_JOBS[tid] = job
    # 任务 id 持久化：页面刷新后能恢复轮询
    st = load_state()
    st["expand_task"] = {"task_id": tid, "status": "queued"}
    save_state(st)

    def work():
        try:
            data = json.loads(_clean_json_text(text))
            if isinstance(data, list):
                data = {"title": "剧本", "storyboards": data}
            role_map = _extract_roles(data)
            storyboards = _extract_storyboards(data)
            job["total"] = len(storyboards)
            job["status"] = "running"
            rows, meta = parse_script_json(text)
            # 断点续跑：读项目现有提示词，已完整段跳过
            st0 = load_state()
            existing = (st0.get("project") or {}).get("prompt_tasks") or []
            prev_p = ""
            for i, sb in enumerate(storyboards):
                scene = str(_pick(sb, "scene", "location", "scene_name") or "").strip()
                job["current"] = f"第 {i + 1}/{len(storyboards)} 段" + (f" · {scene}" if scene else "")
                existing_p = str(existing[i].get("prompt") or "") if i < len(existing) else ""
                if _is_full_prompt(existing_p):
                    rows[i]["prompt"] = existing_p
                    prev_p = existing_p
                    job["done"] = i + 1
                    print(f"[expand] 第 {i + 1} 段已完整，跳过", flush=True)
                    continue
                p = None
                for attempt in range(3):
                    try:
                        p = llm_expand_one(sb, role_map, token, prev_prompt=prev_p)
                        if p:
                            break
                    except Exception as e:
                        print(f"[expand] 第 {i + 1} 段第 {attempt + 1} 次失败：{str(e)[:60]}", flush=True)
                        # 不自动重载 LM Studio（用户要求：重载会反复占用/崩溃本机）
                        time.sleep(3)
                if p:
                    rows[i]["prompt"] = _normalize_llm_prompt(p)
                    prev_p = p
                    _save_expand_progress(rows, tid)  # 每段立即保存
                else:
                    print(f"[expand] 第 {i + 1} 段扩写失败，回退规则", flush=True)
                    own = str(_pick(sb, "prompt", "video_prompt", "text", "description") or "").strip()
                    rows[i]["prompt"] = (
                        enhance_prompt(optimize_prompt_block(own, rows[i]["duration"]), rows[i])
                        if own else compose_storyboard_prompt(sb, role_map, detailed=True)
                    )
                job["done"] = i + 1
            job["tasks"] = rows
            job["meta"] = meta
            job["status"] = "done"
            _save_expand_progress(rows, tid, final=True)
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
        finally:
            job["current"] = ""
    threading.Thread(target=work, daemon=True).start()
    return tid


def _is_full_prompt(p):
    """判定一段提示词是否为完整 AI 扩写（>600 字且三段式/六段式齐全）。
    规则回退模板约 400-470 字，AI 完整版一般 700+ 字。"""
    p = str(p or "")
    return (
        len(p) > 600
        and ("integrated_multimodal_description" in p or "detailed_description" in p)
        and "overall_soundscape" in p
        and "non_diegetic_music" in p
    )


def _save_expand_progress(rows, tid, final=False):
    """扩写进度保存：每段写回项目 prompt_tasks（断点续跑 + 前端可见）。"""
    try:
        st = load_state()
        et = st.get("expand_task") or {}
        if et.get("task_id") != tid:
            return False
        proj = dict(st.get("project") or {})
        proj["prompt_tasks"] = rows
        proj["prompt_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["project"] = proj
        projects = st.get("projects") or {}
        if proj.get("name"):
            snap = dict(proj)
            snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            projects[proj["name"]] = snap
            st["projects"] = projects
        if final:
            st.pop("expand_task", None)
        save_state(st)
        return True
    except Exception:
        return False


def llm_polish_prompts(rows, token=""):
    """把已解析的 H3 提示词交给 LLM 理顺对白逻辑与语言组织。

    返回优化后的任务行。失败抛异常，由调用方回退规则补全。
    """
    items = [
        {
            "id": i + 1,
            "name": r.get("name"),
            "duration_s": r.get("duration"),
            "prompt": r.get("prompt", ""),
        }
        for i, r in enumerate(rows)
    ]
    user_content = (
        "以下是短剧各段 H3 提示词。请重点理顺对白逻辑（前言不搭后语、病句、"
        "语序混乱），让对话自然衔接，同时保持剧情事件、人物关系、场景动作不变。\n"
        + json.dumps(items, ensure_ascii=False, indent=1)
    )
    resp = lmstudio_chat([
        {"role": "system", "content": H3_POLISH_SYSTEM},
        {"role": "user", "content": user_content},
    ], token)
    content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    arr = _extract_json_array(content)
    if not arr:
        raise RuntimeError("LLM 返回内容无法解析为 JSON 数组")
    by_id = {}
    for x in arr:
        try:
            by_id[int(x.get("id"))] = str(x.get("prompt", "")).strip()
        except (TypeError, ValueError, AttributeError):
            continue
    if not by_id:
        raise RuntimeError("LLM 返回 JSON 里没有可用的 prompt")
    for i, r in enumerate(rows):
        p = by_id.get(i + 1)
        if p:
            r["prompt"] = _normalize_llm_prompt(p)
    return rows


def lmstudio_chat(messages, token="", timeout=1800):
    """语言模型统一调用：按配置走本地或云端 OpenAI 兼容接口，主端点失败自动降级备用。"""
    main, backup = _llm_endpoints()
    last_err = None
    for ep in [main, backup]:
        if not ep or not ep.get("url"):
            continue
        try:
            return _chat_once(ep, messages, token, timeout)
        except Exception as e:
            last_err = e
            print(f"[llm] {ep.get('provider')} 调用失败：{e}", flush=True)
    raise RuntimeError(f"语言模型调用失败：{last_err}")


def expand_script_json_llm(text, token=""):
    """LLM 扩写剧本 → (任务行, 元信息)。规则扩写作为兜底。"""
    rows, meta = parse_script_json(text)
    if not rows:
        return rows, meta
    data = json.loads(_clean_json_text(text))
    if isinstance(data, list):
        data = {"title": "剧本", "storyboards": data}
    role_map = _extract_roles(data)
    storyboards = _extract_storyboards(data)
    prompts = llm_expand_storyboards(data, token)
    for i, (sb, row) in enumerate(zip(storyboards, rows)):
        p = prompts.get(i + 1)
        if p:
            row["prompt"] = _normalize_llm_prompt(p)
        else:
            own = str(_pick(sb, "prompt", "video_prompt", "text", "description") or "").strip()
            row["prompt"] = (
                enhance_prompt(optimize_prompt_block(own, row["duration"]), row)
                if own else compose_storyboard_prompt(sb, role_map, detailed=True)
            )
    return rows, meta


def _normalize_llm_prompt(p):
    """兜底：保证 LLM 输出符合 H3 三段式（漏前缀/漏字段时补齐）。"""
    p = str(p or "").strip()
    if not p:
        return p
    p = _renumber_shot_numbers(p)
    if not p.startswith("integrated_multimodal_description") and p.lstrip().startswith("["):
        p = "integrated_multimodal_description: " + p.lstrip()
    if "overall_soundscape:" not in p:
        p = p.rstrip() + "\n\noverall_soundscape: 自然的环境声，清晰的人声对白。"
    if "non_diegetic_music:" not in p:
        p = p.rstrip() + "\n\nnon_diegetic_music: N/A"
    return p.strip()


def _renumber_shot_numbers(prompt):
    """把画面描述字段里的 [Shot N] 按出现顺序重新编号为 1,2,3…（第一镜必须是 [Shot 1]）。
    只处理 integrated_multimodal_description / detailed_description 正文，
    不动 I2V 首帧对齐头里的「来自 [Shot 1]」引用。"""
    m = re.search(
        r"((?:integrated_multimodal_description|detailed_description):\s*)(.*?)(?=\n\s*(?:overall_soundscape|non_diegetic_music):|\Z)",
        prompt, re.S,
    )
    if not m:
        return prompt
    counter = [0]

    def repl(mm):
        counter[0] += 1
        return f"[Shot {counter[0]}]"

    new_body = re.sub(r"\[Shot \d+\]", repl, m.group(2))
    return prompt[:m.start(2)] + new_body + prompt[m.end(2):]


def parse_script_json(text):
    """剧本 JSON → (任务行, 元信息)。

    支持的剧本形状：
    - Novel-Director：{title, role_list:[{role_name, role_desc, avatar}],
                       storyboard_list:[{id, role_list, scene, camera, dialogue, duration, video_prompt}]}
    - ArcReel：{title, characters:[{name, description, image}],
                segments:[{index, characters, location, prompt, dialogue, duration}]}
    - 通用分镜：{title, scenes:[{scene, roles, action, dialogue, duration, prompt}]}

    元信息：role_images（前两个角色参考图）、scene_image、warnings（未匹配提示）。
    """
    text = _clean_json_text(text)
    if not text:
        return [], {"role_images": [], "scene_image": None, "warnings": ["剧本内容是空的"]}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        snippet = text[max(0, e.pos - 25):e.pos + 25].replace("\n", "⏎")
        return [], {
            "role_images": [], "scene_image": None,
            "warnings": [
                f"JSON 格式错误（第 {e.lineno} 行第 {e.colno} 列）："
                f"…{snippet}… 请检查：引号是否为英文半角、是否多了/少了逗号、"
                f"或内容是否被 ``` 代码块包裹（会自动剥掉）"
            ],
        }
    if isinstance(data, list):
        data = {"title": "剧本", "storyboards": data}
    if not isinstance(data, dict):
        return [], {"role_images": [], "scene_image": None, "warnings": ["剧本 JSON 必须是对象或数组"]}

    title = str(_pick(data, "title", "name", "script_name") or "").strip() or "剧本"
    base_slug = _slug(title) or "script"
    role_map = _extract_roles(data)
    storyboards = _extract_storyboards(data)
    if not storyboards:
        return [], {
            "role_images": [], "scene_image": None,
            "warnings": ["剧本里没有找到分镜（storyboard_list / segments / scenes）"],
        }

    # 角色参考图（显式 avatar > 关键词匹配），最多两个，作为全局链式参考
    role_images = []
    seen = set()
    for rn, r in role_map.items():
        img = ""
        if r.get("avatar") and find_image(r["avatar"]):
            img = r["avatar"]
        else:
            img = match_role_image(rn, r.get("role_desc")) or ""
        if img and img not in seen:
            seen.add(img)
            role_images.append(img)
        if len(role_images) >= 2:
            break

    scene_image = None
    rows = []
    warnings = []
    for i, sb in enumerate(storyboards):
        dur = _sb_duration(sb)
        scene_name = str(_pick(sb, "scene", "location", "place", "scene_name") or "").strip()
        anchor = match_scene(scene_name) if scene_name else None
        if anchor and anchor.get("image") and find_image(anchor["image"]):
            scene_image = scene_image or anchor["image"]
        row_scene_img = (
            anchor["image"] if anchor and anchor.get("image") and find_image(anchor["image"]) else ""
        )

        sb_role_names = _sb_roles(sb)
        sb_role_imgs = []
        for rn in sb_role_names:
            r = role_map.get(rn)
            img = ""
            if r and r.get("avatar") and find_image(r["avatar"]):
                img = r["avatar"]
            else:
                img = match_role_image(rn, r.get("role_desc") if r else "") or ""
            if img and img not in sb_role_imgs:
                sb_role_imgs.append(img)

        prompt = str(_pick(sb, "prompt", "video_prompt", "text", "description") or "").strip()
        if prompt:
            prompt = optimize_prompt_block(prompt, dur)
        else:
            prompt = compose_storyboard_prompt(sb, role_map)

        # 有角色/场景参考 → R2V（利用全局角色图 + 场景图）；否则 T2V
        mode = "r2v" if (role_images or scene_image) else "t2v"
        rows.append({
            "name": f"{title}_{i + 1:02d}",
            "mode": mode,
            "duration": dur,
            "mp": 1.0,
            "image": row_scene_img,
            "images": list(dict.fromkeys(sb_role_imgs + ([row_scene_img] if row_scene_img else [])))[:4],
            "prefix": f"video/{base_slug}_{i + 1:02d}",
            "prompt": prompt,
        })
        for rn in sb_role_names:
            if rn not in role_map:
                warnings.append(f"第 {i + 1} 段角色「{rn}」未在角色表中定义")
            elif not (match_role_image(rn, role_map[rn].get("role_desc")) or
                      (role_map[rn].get("avatar") and find_image(role_map[rn]["avatar"]))):
                warnings.append(f"角色「{rn}」未匹配到参考图（建议在素材目录放一张 {rn} 的锚点图）")
    if len(role_map) > 2:
        warnings.append(f"剧本共 {len(role_map)} 个角色，链式 R2V 只取前两个角色的参考图，其余角色建议每段单独配图")
    if not role_images:
        warnings.append("未匹配到任何角色参考图，全部段将用 T2V 生成（建议先上传角色锚点图）")

    meta = {
        "role_images": role_images,
        "scene_image": scene_image,
        "warnings": list(dict.fromkeys(warnings)),
    }
    return rows, meta


def _clean_json_text(text):
    """清理用户粘贴的剧本文本：去 BOM、剥 markdown 代码块、去首尾空白。"""
    s = str(text or "").strip()
    if not s:
        return s
    if s.startswith("\ufeff"):
        s = s[1:].strip()
    m = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    return s


def _looks_like_prompt_blocks(text):
    """判断文本是不是 H3 Prompt 分块格式（而非剧本 JSON）。"""
    t = str(text or "").strip().lstrip("\ufeff")
    if not t:
        return False
    if t.startswith("{") or t.startswith("["):
        return False
    if t.startswith("integrated_multimodal_description"):
        return True
    if re.search(r"Prompt\s*\d+\s*[（(]", t):
        return True
    if re.search(r"^(integrated_multimodal_description|overall_soundscape|non_diegetic_music):", t, re.M):
        return True
    return False


def tasks_to_csv(tasks):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for t in tasks:
        row = {k: t.get(k, "") for k in CSV_FIELDS}
        if isinstance(row.get("images"), list):
            row["images"] = "|".join(row["images"])
        writer.writerow(row)
    return buf.getvalue()


def clear_history(keep=None):
    """清空服务端任务历史（保留名单可选）。"""
    state = load_state()
    if keep:
        state["tasks"] = [t for t in state["tasks"] if t.get("id") in keep]
    else:
        state["tasks"] = []
    save_state(state)
    return len(state["tasks"])


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "ComfyUIBatchConsole/1.0"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {fmt % args}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_media(self, p, ctype):
        """支持 HTTP Range 的媒体文件发送（视频必须，否则浏览器只能播开头几秒）。"""
        size = os.path.getsize(p)
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", str(rng).strip())
            if m:
                s, e = m.group(1), m.group(2)
                if s == "" and e == "":
                    start, end = 0, size - 1
                elif s == "":
                    length = int(e)
                    start = max(0, size - length)
                    end = size - 1
                else:
                    start = int(s)
                    end = int(e) if e else size - 1
                    if end >= size:
                        end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                with open(p, "rb") as f:
                    f.seek(start)
                    data = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path == "/":
            if not os.path.exists(INDEX_FILE):
                self._send(404, "index.html 不存在", "text/plain; charset=utf-8")
                return
            with open(INDEX_FILE, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/api/status":
            qs = urllib.parse.parse_qs(path.query)
            server = qs.get("server", [DEFAULT_SERVER])[0]
            self._send(200, json.dumps(get_status(server), ensure_ascii=False))
            return
        if path.path == "/api/images":
            self._send(200, json.dumps({"images": list_images()}, ensure_ascii=False))
            return
        if path.path == "/api/media":
            self._send(200, json.dumps({"media": list_media()}, ensure_ascii=False))
            return
        if path.path == "/api/scan_assets":
            self._send(200, json.dumps({"assets": scan_assets()}, ensure_ascii=False))
            return
        if path.path == "/api/assemble_progress":
            qs = urllib.parse.parse_qs(path.query)
            tid = qs.get("task_id", [""])[0]
            job = ASSEMBLE_JOBS.get(tid)
            if not job:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({
                "status": job["status"], "message": job["message"],
                "filename": job["filename"], "error": job["error"],
                "done": job.get("done", 0), "total": job.get("total", 0),
                "skipped": job.get("skipped", []),
            }, ensure_ascii=False))
            return
        if path.path == "/api/assemble_versions":
            """返回项目每段可用的视频版本列表（供合成时手动选择）。"""
            st = load_state()
            proj = st.get("project") or {}
            pt = proj.get("prompt_tasks") or []
            all_tasks = st.get("tasks", [])
            segs = []
            for seg in pt:
                sname = str(seg.get("name") or "")
                if not sname:
                    continue
                cands = [
                    t for t in all_tasks
                    if (t.get("name") == sname or str(t.get("name") or "").startswith(sname + "_"))
                    and t.get("output_file") and t.get("downloaded")
                ]
                cands.sort(key=lambda x: str(x.get("submitted_at") or ""))
                segs.append({
                    "name": sname,
                    "versions": [
                        {
                            "filename": t["output_file"].get("filename"),
                            "task_name": t.get("name"),
                            "submitted_at": t.get("submitted_at"),
                            "quality": t.get("quality"),
                            "steps": t.get("steps"),
                            "mode": t.get("mode"),
                        }
                        for t in cands
                    ],
                })
            self._send(200, json.dumps({"segments": segs}, ensure_ascii=False))
            return
        if path.path == "/api/assemble_history":
            """已合成视频历史（素材目录下 合成_*.mp4，按时间倒序）。"""
            files = []
            for d in IMAGE_DIRS:
                if not os.path.isdir(d):
                    continue
                for fn in os.listdir(d):
                    if fn.startswith("合成_") and fn.lower().endswith(".mp4"):
                        p = os.path.join(d, fn)
                        try:
                            size = os.path.getsize(p)
                            mtime = time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(os.path.getmtime(p)),
                            )
                        except OSError:
                            continue
                        files.append({"filename": fn, "size": size, "mtime": mtime})
            files.sort(key=lambda x: x["mtime"], reverse=True)
            self._send(200, json.dumps({"history": files}, ensure_ascii=False))
            return
        if path.path == "/api/task":
            """返回任务完整详情（重新生成配置弹窗用）。"""
            qs = urllib.parse.parse_qs(path.query)
            task_id = qs.get("task_id", [""])[0]
            st = load_state()
            t = next((x for x in st.get("tasks", []) if x.get("id") == task_id), None)
            if not t:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"task": t}, ensure_ascii=False))
            return
        if path.path == "/api/llm_check":
            self._send(200, json.dumps(check_lmstudio(), ensure_ascii=False))
            return
        if path.path == "/api/config":
            # 用户运行时保存的服务器优先于配置文件（防止配置化把已连通的地址改丢）
            cfg = _CONFIG
            try:
                st0 = load_state()
                if st0.get("server"):
                    cfg = dict(cfg)
                    cfg["comfyui"] = dict(cfg.get("comfyui") or {})
                    cfg["comfyui"]["server"] = st0["server"]
            except Exception:
                pass
            self._send(200, json.dumps(cfg, ensure_ascii=False))
            return
        if path.path == "/api/boogu_check":
            self._send(200, json.dumps(boogu_check(), ensure_ascii=False))
            return
        if path.path.startswith("/media/"):
            fn = urllib.parse.unquote(path.path[len("/media/"):])
            for d in IMAGE_DIRS:
                p = os.path.join(d, fn)
                if os.path.isfile(p):
                    ctype = "image/png"
                    if fn.lower().endswith((".jpg", ".jpeg")):
                        ctype = "image/jpeg"
                    elif fn.lower().endswith(".webp"):
                        ctype = "image/webp"
                    elif fn.lower().endswith(".mp4"):
                        ctype = "video/mp4"
                    if ctype.startswith("video/"):
                        self._send_media(p, ctype)
                    else:
                        with open(p, "rb") as f:
                            data = f.read()
                        self._send(200, data, ctype)
                    return
            # 生成视频：递归查 outputs 目录
            if os.path.isdir(OUTPUTS_DIR):
                for root, _, files in os.walk(OUTPUTS_DIR):
                    if fn in files:
                        p = os.path.join(root, fn)
                        ctype = "video/mp4"
                        if fn.lower().endswith(".webm"):
                            ctype = "video/webm"
                        elif fn.lower().endswith(".mov"):
                            ctype = "video/quicktime"
                        self._send_media(p, ctype)
                        return
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        if path.path == "/api/records":
            rows = []
            if os.path.exists(RECORDS_CSV):
                with open(RECORDS_CSV, "r", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
            # 倒序：最新记录在最上方
            self._send(200, json.dumps({"records": rows[-50:][::-1]}, ensure_ascii=False))
            return
        if path.path == "/api/rules":
            rules = []
            for name, (_, title) in RULES.items():
                rules.append({
                    "name": name, "title": title,
                    "content": globals()[RULES[name][0]],
                })
            self._send(200, json.dumps({"rules": rules}, ensure_ascii=False))
            return
        if path.path == "/api/expand_progress":
            qs = urllib.parse.parse_qs(path.query)
            tid = qs.get("task_id", [""])[0]
            job = EXPAND_JOBS.get(tid)
            if not job:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                return
            resp = {
                "status": job["status"], "done": job["done"], "total": job["total"],
                "current": job["current"], "error": job["error"],
            }
            if job["status"] in ("done", "error"):
                resp["tasks"] = job["tasks"]
                resp["meta"] = job["meta"]
            self._send(200, json.dumps(resp, ensure_ascii=False))
            return
        if path.path == "/api/expand_current":
            st = load_state()
            et = st.get("expand_task")
            if not et:
                self._send(200, json.dumps({"active": False}, ensure_ascii=False))
                return
            job = EXPAND_JOBS.get(et.get("task_id"))
            if not job:
                st.pop("expand_task", None)
                save_state(st)
                self._send(200, json.dumps({"active": False, "lost": True}, ensure_ascii=False))
                return
            self._send(200, json.dumps({
                "active": True, "task_id": et["task_id"],
                "status": job["status"], "done": job["done"], "total": job["total"],
                "current": job["current"], "error": job["error"],
            }, ensure_ascii=False))
            return
        if path.path == "/api/assets_gen_progress":
            qs = urllib.parse.parse_qs(path.query)
            tid = qs.get("task_id", [""])[0]
            job = ASSET_JOBS.get(tid)
            if not job:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({
                "status": job["status"], "done": job["done"], "total": job["total"],
                "current": job["current"], "error": job["error"], "failed": job.get("failed", []),
            }, ensure_ascii=False))
            return
        if path.path == "/api/assets_gen_current":
            st = load_state()
            et = st.get("assets_gen_task")
            if not et:
                self._send(200, json.dumps({"active": False}, ensure_ascii=False))
                return
            job = ASSET_JOBS.get(et.get("task_id"))
            if not job:
                st.pop("assets_gen_task", None)
                save_state(st)
                self._send(200, json.dumps({"active": False, "lost": True}, ensure_ascii=False))
                return
            self._send(200, json.dumps({
                "active": True, "task_id": et["task_id"],
                "status": job["status"], "done": job["done"], "total": job["total"],
                "current": job["current"], "error": job["error"], "failed": job.get("failed", []),
            }, ensure_ascii=False))
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json()
        except Exception:
            self._send(400, json.dumps({"error": "JSON 解析失败"}, ensure_ascii=False))
            return
        if path == "/api/check":
            server = body.get("server", DEFAULT_SERVER)
            self._send(200, json.dumps(check_server(server), ensure_ascii=False))
            return
        if path == "/api/boogu_gen":
            prompt = body.get("prompt", "")
            filename = body.get("filename", "")
            size = body.get("size", "768x1024")
            if not prompt.strip() or not filename.strip():
                self._send(400, json.dumps({"error": "缺少 prompt 或 filename"}, ensure_ascii=False))
                return
            try:
                fn, dest = boogu_generate(prompt, filename, size)
            except Exception as e:
                self._send(400, json.dumps({"error": f"Boogu 生图失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"filename": fn, "path": dest}, ensure_ascii=False))
            return
        if path == "/api/assets_gen_batch":
            """服务端后台批量生图（一键生成用，前端轮询进度，刷新可恢复）。"""
            items = body.get("items") or []
            if not items:
                self._send(400, json.dumps({"error": "没有待生成资产"}, ensure_ascii=False))
                return
            tid = start_assets_batch_job(items)
            self._send(200, json.dumps({"task_id": tid}, ensure_ascii=False))
            return
        if path == "/api/asset_gen":
            prompt = body.get("prompt", "")
            filename = body.get("filename", "")
            kind = body.get("kind", "story")
            key = body.get("key")
            expected = body.get("expected") or {}
            if not prompt.strip() or not filename.strip():
                self._send(400, json.dumps({"error": "缺少 prompt 或 filename"}, ensure_ascii=False))
                return
            # 分镜图走参考编辑模式：场景图 + 出场角色锚点（与批量任务一致）
            refs, mode = None, None
            if kind == "story":
                st0 = load_state()
                proj = dict(st0.get("project") or {})
                refs = _story_refs(proj, key)
                still_cfg = _still_cfg()
                mode = str(still_cfg.get("story_mode") or "refedit")
            attempts = 0
            last_fn, last_issues = None, []
            max_tries = 8 if kind == "story" else 3
            while attempts < max_tries:
                try:
                    fn, dest = boogu_generate(prompt, filename, "768x1024", refs=refs, mode=mode)
                except Exception as e:
                    self._send(400, json.dumps({"error": f"Boogu 生图失败：{e}"}, ensure_ascii=False))
                    return
                attempts += 1
                v = verify_asset(dest, kind, expected)
                last_fn, last_issues = fn, v.get("issues", [])
                if v.get("ok"):
                    self._send(200, json.dumps({
                        "filename": fn, "verified": True, "issues": [],
                        "attempts": attempts,
                    }, ensure_ascii=False))
                    return
                # 换文件名重试（保留失败版本供对比）
                filename = re.sub(
                    r"_([a-z0-9]+)(\.(?:png|jpg|jpeg|webp))$",
                    lambda m: f"_{int(time.time() * 1000):x}{m.group(2)}", fn,
                )
            self._send(200, json.dumps({
                "filename": last_fn, "verified": False, "issues": last_issues,
                "attempts": attempts,
            }, ensure_ascii=False))
            return
        if path == "/api/asset_reverify":
            """给已有参考图补质检：遍历当前项目 asset_imgs，对缺质检记录的图
            跑 verify_asset，结果写入 asset_meta（老项目升级后一键补检）。"""
            st = load_state()
            proj = dict(st.get("project") or {})
            ai = proj.get("asset_imgs") or {}
            am = dict(proj.get("asset_meta") or {})
            am.setdefault("role", {})
            am.setdefault("scene", {})
            am.setdefault("story", {})
            ast = proj.get("asset_state") or {}
            results = []
            # 角色
            for name, fn in (ai.get("role") or {}).items():
                if isinstance(am["role"].get(name), dict) and am["role"].get(name, {}).get("verified"):
                    continue
                lp = find_image(str(fn))
                if not lp:
                    continue
                r = ast.get("roles", {}).get(name) or {}
                expected = {
                    "gender": (ai.get("roleGender") or {}).get(name)
                              or (lambda: "女" if "女" in str(r.get("identity") or r.get("costume") or "") else "未知")(),
                    "hair": r.get("hair") or "",
                    "costume": r.get("costume") or "",
                    "look": "",
                }
                v = verify_asset(lp, "role", expected)
                am["role"][name] = {"verified": v.get("ok"), "issues": v.get("issues", []), "attempts": 1}
                results.append({"kind": "role", "key": name, **v})
            # 场景
            for name, fn in (ai.get("scene") or {}).items():
                if isinstance(am["scene"].get(name), dict) and am["scene"].get(name, {}).get("verified"):
                    continue
                lp = find_image(str(fn))
                if not lp:
                    continue
                s = ast.get("scenes", {}).get(name) or {}
                v = verify_asset(lp, "scene", {"desc": s.get("desc") or ""})
                am["scene"][name] = {"verified": v.get("ok"), "issues": v.get("issues", []), "attempts": 1}
                results.append({"kind": "scene", "key": name, **v})
            # 分镜
            for idx, fn in (ai.get("story") or {}).items():
                if isinstance(am["story"].get(idx), dict) and am["story"].get(idx, {}).get("verified"):
                    continue
                lp = find_image(str(fn))
                if not lp:
                    continue
                v = verify_asset(lp, "story", {"look": "", "people_count": None})
                am["story"][idx] = {"verified": v.get("ok"), "issues": v.get("issues", []), "attempts": 1}
                results.append({"kind": "story", "key": idx, **v})
            proj["asset_meta"] = am
            st["project"] = proj
            projects = st.get("projects") or {}
            if proj.get("name"):
                snap = dict(proj)
                snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                projects[proj["name"]] = snap
                st["projects"] = projects
            save_state(st)
            self._send(200, json.dumps({"results": results, "meta": am}, ensure_ascii=False))
            return
        if path == "/api/upload_media":
            filename = str(body.get("filename") or "").strip()
            data_b64 = body.get("data_b64", "")
            if not filename or not data_b64:
                self._send(400, json.dumps({"error": "缺少文件名或内容"}, ensure_ascii=False))
                return
            try:
                raw = base64.b64decode(data_b64)
                dest = os.path.join(IMAGE_DIRS[0], filename)
                with open(dest, "wb") as f:
                    f.write(raw)
            except Exception as e:
                self._send(400, json.dumps({"error": f"上传失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"filename": filename}, ensure_ascii=False))
            return
        if path == "/api/llm_check":
            token = _lm_token(body)
            if body.get("lmstudio_token"):
                st = load_state()
                st["lmstudio_token"] = str(body["lmstudio_token"]).strip()
                save_state(st)
            if body.get("lmstudio_model"):
                st = load_state()
                st["lmstudio_model"] = str(body["lmstudio_model"]).strip()
                save_state(st)
            self._send(200, json.dumps(check_lmstudio(token), ensure_ascii=False))
            return
        if path == "/api/config":
            """保存配置：前端设置抽屉 → 写 config.json（自动备份旧文件）。"""
            new_cfg = body.get("config")
            if not isinstance(new_cfg, dict):
                self._send(400, json.dumps({"error": "缺少 config"}, ensure_ascii=False))
                return
            try:
                p = _config_path()
                if os.path.isfile(p):
                    shutil.copy(p, p + ".bak")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(new_cfg, f, ensure_ascii=False, indent=2)
                global _CONFIG, _VISION_ENV
                _CONFIG = load_config()
                # 端点/视觉读取 _CONFIG，保存后立即生效；静态常量重启后同步
                _VISION_ENV = None
            except Exception as e:
                self._send(400, json.dumps({"error": f"配置保存失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"saved": True, "config": _CONFIG}, ensure_ascii=False))
            return
        if path == "/api/submit":
            server = body.get("server", DEFAULT_SERVER)
            tasks = body.get("tasks", [])
            auto = bool(body.get("auto_download", True))
            chain = bool(body.get("chain_mode", False))
            role_images = body.get("role_images") or []
            scene_image = body.get("scene_image") or None
            if not tasks:
                self._send(400, json.dumps({"error": "没有任务"}, ensure_ascii=False))
                return
            results, err, warnings = submit_tasks(server, tasks, auto, chain, role_images, scene_image)
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                try:
                    st = load_state()
                    proj = dict(st.get("project") or {})
                    if proj.get("name"):
                        names = [str(t.get("name") or "") for t in tasks if t.get("name")]
                        prev = proj.get("submitted_task_names") or []
                        proj["submitted_task_names"] = list(dict.fromkeys(list(prev) + names))
                        proj["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        # 生成记录（P2 迭代可追溯：每次提交 = 一个版本批次）
                        gen_log = proj.get("generation_log") or []
                        gen_log.append({
                            "at": proj["submitted_at"],
                            "count": len(tasks),
                            "ok": sum(1 for r in results if r.get("ok")),
                            "total": len(results),
                            "chain_mode": bool(chain),
                            "shots": [
                                {
                                    "name": str(t.get("name") or ""),
                                    "mode": t.get("mode"),
                                    "duration": t.get("duration"),
                                    "mp": t.get("mp"),
                                    "quality": t.get("quality"),
                                    "steps": t.get("steps"),
                                    "prompt_fp": prompt_fingerprint(t.get("prompt")),
                                }
                                for t in tasks
                            ],
                        })
                        proj["generation_log"] = gen_log[-100:]
                        st["project"] = proj
                        projects = st.get("projects") or {}
                        snap = dict(proj)
                        snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        projects[proj["name"]] = snap
                        st["projects"] = projects
                        save_state(st)
                except Exception:
                    pass
                self._send(200, json.dumps({"results": results, "warnings": warnings}, ensure_ascii=False))
            return
        if path == "/api/import":
            fmt = body.get("format", "csv")
            text = body.get("text", "")
            if fmt == "csv" and re.search(r"Prompt\s*\d+\s*[（(]", text) and re.search(r"(?i)\bcopy\b|integrated_multimodal_description", text):
                fmt = "prompts"
            try:
                rows = parse_tasks_text(text, fmt)
            except Exception as e:
                self._send(400, json.dumps({"error": f"解析失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"tasks": rows}, ensure_ascii=False))
            return
        if path == "/api/import_script":
            text = body.get("text", "")
            if _looks_like_prompt_blocks(text):
                rows = parse_prompt_blocks(text)
                self._send(200, json.dumps({"tasks": rows, "detected": "prompts"}, ensure_ascii=False))
                return
            try:
                rows, meta = parse_script_json(text)
            except Exception as e:
                self._send(400, json.dumps({"error": f"剧本解析失败：{e}"}, ensure_ascii=False))
                return
            if not rows:
                self._send(400, json.dumps({"error": meta.get("warnings", ["剧本为空"])[0]}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"tasks": rows, **meta}, ensure_ascii=False))
            return
        if path == "/api/expand_script":
            text = body.get("text", "")
            if _looks_like_prompt_blocks(text):
                rows = parse_prompt_blocks(text)
                use_llm_p = bool(body.get("use_llm", True))
                token_p = _lm_token(body)
                if body.get("lmstudio_token"):
                    st = load_state()
                    st["lmstudio_token"] = str(body["lmstudio_token"]).strip()
                    save_state(st)
                llm_p = {"used": False}
                if use_llm_p and rows and check_lmstudio(token_p).get("available"):
                    try:
                        rows = llm_polish_prompts(rows, token_p)
                        llm_p = {"used": True}
                    except Exception as e:
                        llm_p = {"used": False, "fallback": str(e)}
                        print(f"[expand] Prompt 块 AI 润色失败，回退规则：{e}")
                if not llm_p.get("used"):
                    for r in rows:
                        r["prompt"] = enhance_prompt(r.get("prompt", ""), r)
                print(f"[expand] Prompt 块 {len(rows)} 段，llm.used={llm_p.get('used')} fallback={llm_p.get('fallback', '')}")
                self._send(200, json.dumps(
                    {"tasks": rows, "detected": "prompts", "llm": llm_p},
                    ensure_ascii=False,
                ))
                return
            use_llm = bool(body.get("use_llm", True))
            token = _lm_token(body)
            if body.get("lmstudio_token"):
                st = load_state()
                st["lmstudio_token"] = str(body["lmstudio_token"]).strip()
                save_state(st)
            if body.get("async") and use_llm:
                tid = start_expand_job(text, token)
                self._send(200, json.dumps({"task_id": tid, "async": True}, ensure_ascii=False))
                return
            llm_info = {"used": False}
            try:
                if use_llm and check_lmstudio(token).get("available"):
                    try:
                        rows, meta = expand_script_json_llm(text, token)
                        llm_info = {"used": True}
                    except Exception as e:
                        rows, meta = expand_script_json(text)
                        llm_info = {"used": False, "fallback": str(e)}
                        print(f"[expand] 剧本 AI 扩写失败，回退规则：{e}")
                else:
                    rows, meta = expand_script_json(text)
                    if use_llm:
                        llm_info = {"used": False, "fallback": "LM Studio 不可用、token 无效或模型未加载"}
                print(f"[expand] 剧本 {len(rows)} 段，llm.used={llm_info.get('used')} fallback={llm_info.get('fallback', '')}")
            except Exception as e:
                self._send(400, json.dumps({"error": f"剧本扩写失败：{e}"}, ensure_ascii=False))
                return
            if not rows:
                self._send(400, json.dumps({"error": meta.get("warnings", ["剧本为空"])[0]}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"tasks": rows, "expanded": True, "llm": llm_info, **meta}, ensure_ascii=False))
            return
        if path == "/api/generate_script":
            topic = body.get("topic", "")
            style = body.get("style", "")
            segments = int(body.get("segments") or 10)
            token = _lm_token(body)
            if body.get("lmstudio_token"):
                st = load_state()
                st["lmstudio_token"] = str(body["lmstudio_token"]).strip()
                save_state(st)
            if not topic.strip():
                self._send(400, json.dumps({"error": "请填写剧情梗概/题材"}, ensure_ascii=False))
                return
            try:
                script = llm_generate_script(topic, style, segments, token)
            except Exception as e:
                self._send(400, json.dumps({"error": f"剧本生成失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"script": script}, ensure_ascii=False))
            return
        if path == "/api/rewrite_script":
            script = body.get("script")
            token = _lm_token(body)
            if not isinstance(script, dict) or not script.get("storyboard_list"):
                self._send(400, json.dumps({"error": "请先提供剧本 JSON"}, ensure_ascii=False))
                return
            try:
                new_script = llm_rewrite_script(script, token)
            except Exception as e:
                self._send(400, json.dumps({"error": f"剧本改写失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"script": new_script}, ensure_ascii=False))
            return
        if path == "/api/parse_script_text":
            text = body.get("text", "")
            token = _lm_token(body)
            try:
                segments = int(body.get("segments") or 0)
            except (TypeError, ValueError):
                segments = 0
            if body.get("lmstudio_token"):
                st = load_state()
                st["lmstudio_token"] = str(body["lmstudio_token"]).strip()
                save_state(st)
            if not str(text or "").strip():
                self._send(400, json.dumps({"error": "请粘贴剧本内容"}, ensure_ascii=False))
                return
            try:
                script = llm_text_to_script(text, token, segments)
            except Exception as e:
                self._send(400, json.dumps({"error": f"剧本转换失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"script": script}, ensure_ascii=False))
            return
        if path == "/api/project":
            st = load_state()
            projects = st.get("projects") or {}
            cur = dict(st.get("project") or {})
            # 老版本只有 project 没有项目库：首次运行时登记当前项目
            if not projects and cur.get("name"):
                snap = dict(cur)
                snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                projects[cur["name"]] = snap
            if body.get("name") is not None:
                new_name = str(body.get("name") or "").strip() or "未命名项目"
                old_name = str(cur.get("name") or "")
                if old_name and old_name != new_name:
                    snap = dict(cur)
                    snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    projects[old_name] = snap  # 切换前先归档当前项目
                if bool(body.get("fresh")):
                    if new_name in projects:
                        self._send(400, json.dumps({"error": f"项目名「{new_name}」已存在，请换一个"}, ensure_ascii=False))
                        return
                    proj = {"name": new_name}  # 明确新建：不继承旧工作数据
                elif new_name != old_name and new_name in projects:
                    proj = dict(projects[new_name])  # 切回已存在项目
                elif new_name != old_name:
                    proj = {"name": new_name}  # 新名字：空项目
                else:
                    proj = cur
                for k in ("name", "type", "aspect", "budget"):
                    if body.get(k) is not None:
                        proj[k] = str(body.get(k) or "")
                for k in ("script_before", "script_after", "current_script", "prompt_tasks",
                          "role_images", "scene_image", "chain_mode",
                          "asset_imgs", "asset_prompts", "asset_meta",
                          "asset_state", "generation_log"):
                    if k in body:
                        proj[k] = body[k]
                if body.get("prompt_tasks") is not None:
                    proj["prompt_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                proj["name"] = new_name
                st["project"] = proj
                snap2 = dict(proj)
                snap2["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                projects[new_name] = snap2
                st["projects"] = projects
                save_state(st)
            self._send(200, json.dumps({"project": st.get("project") or {}}, ensure_ascii=False))
            return
        if path == "/api/projects":
            st = load_state()
            projects = st.get("projects") or {}
            cur0 = st.get("project") or {}
            if not projects and cur0.get("name"):
                snap = dict(cur0)
                snap["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                projects[cur0["name"]] = snap
                st["projects"] = projects
                save_state(st)
            cur_name = str((st.get("project") or {}).get("name") or "")
            tasks = st.get("tasks") or []
            rows = []
            for name, p in projects.items():
                # 状态机判定（每档只有达到该档的硬条件才进位，资产档必须有质检通过记录）
                def _sb_ready(obj):
                    return isinstance(obj, dict) and bool(obj.get("storyboard_list"))

                def _any_verified(meta_dict):
                    if not isinstance(meta_dict, dict):
                        return False
                    return any(
                        isinstance(v, dict) and bool(v.get("verified"))
                        for v in meta_dict.values()
                    )

                progress = 0
                if _sb_ready(p.get("current_script")) or _sb_ready(p.get("script_before")):
                    progress = 1
                if _sb_ready(p.get("script_after")):
                    progress = 2
                if (p.get("prompt_tasks") or []):
                    progress = 3
                meta = p.get("asset_meta") or {}
                ai = p.get("asset_imgs") or {}
                if any(
                    isinstance(ai.get(k), dict) and any(v for v in ai[k].values() if v)
                    for k in ("role", "scene", "story")
                ):
                    progress = 4
                prefixes = [name + "_"]
                for k in ("current_script", "script_before", "script_after"):
                    s = p.get(k) or {}
                    if isinstance(s, dict) and s.get("title"):
                        prefixes.append(str(s["title"]) + "_")
                        break
                matched = [
                    t for t in tasks
                    if any(str(t.get("name") or "").startswith(pre) for pre in prefixes)
                ]
                if p.get("submitted_at") or p.get("submitted_task_names") or matched:
                    progress = 5
                # 段级出片：每段只要有同名任务完整出片过（下载过视频）就算完成；
                # 重跑/二次生成是独立版本（记入 generation_log），不回退项目进度
                pt_segs = p.get("prompt_tasks") or []
                seg_done = 0
                for seg in pt_segs:
                    sname = str(seg.get("name") or "")
                    if not sname:
                        continue
                    sc = [t for t in tasks
                          if t.get("name") == sname
                          or str(t.get("name") or "").startswith(sname + "_")]
                    if not sc:
                        continue
                    if any(t.get("downloaded") or t.get("output_file") for t in sc):
                        seg_done += 1
                seg_total = len(pt_segs)
                if progress >= 5 and seg_total and seg_done == seg_total:
                    progress = 6
                # 提示词完成度：完整扩写（>800 字且三段式）的段数
                prompt_done = sum(1 for s in pt_segs if _is_full_prompt(s.get("prompt")))
                rows.append({
                    "name": name,
                    "type": p.get("type", ""),
                    "aspect": p.get("aspect", ""),
                    "budget": p.get("budget", ""),
                    "updated_at": p.get("updated_at", ""),
                    "progress": progress,
                    "stage": PROGRESS_LABELS[progress] if 0 <= progress < len(PROGRESS_LABELS) else "",
                    "asset_ok": progress >= 4,
                    "verified_counts": {
                        k: sum(
                            1 for v in (meta.get(k) or {}).values()
                            if isinstance(v, dict) and v.get("verified")
                        ) if isinstance(meta.get(k), dict) else 0
                        for k in ("role", "scene", "story")
                    },
                    "seg_done": seg_done,
                    "seg_total": seg_total,
                    "prompt_done": prompt_done,
                    "prompt_total": seg_total,
                    "has_script": bool(p.get("current_script") or p.get("script_before")),
                    "has_prompt": bool(p.get("prompt_tasks")),
                    "current": name == cur_name,
                })
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            self._send(200, json.dumps({"projects": rows}, ensure_ascii=False))
            return
        if path == "/api/project/open":
            name = str(body.get("name") or "")
            st = load_state()
            projects = st.get("projects") or {}
            if name not in projects:
                self._send(404, json.dumps({"error": "项目不存在"}, ensure_ascii=False))
                return
            proj = dict(projects[name])
            st["project"] = proj
            save_state(st)
            self._send(200, json.dumps({"project": proj}, ensure_ascii=False))
            return
        if path == "/api/rule":
            name = body.get("name", "")
            content = body.get("content", "")
            reset = bool(body.get("reset", False))
            try:
                save_rule(name, content, reset=reset)
            except Exception as e:
                self._send(400, json.dumps({"error": f"规则保存失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"saved": True, "name": name}, ensure_ascii=False))
            return
        if path == "/api/asset_prompt":
            current = body.get("current_prompt", "")
            instruction = body.get("instruction", "")
            token = _lm_token(body)
            if not current.strip() or not instruction.strip():
                self._send(400, json.dumps({"error": "缺少提示词或修改要求"}, ensure_ascii=False))
                return
            try:
                resp = lmstudio_chat([
                    {"role": "system", "content": ASSET_PROMPT_SYSTEM},
                    {"role": "user", "content":
                        f"当前提示词：\n{current}\n\n修改要求：{instruction}\n\n请输出修改后的完整提示词。"},
                ], token)
                content = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                content = content.strip('"').strip()
                if not content:
                    raise RuntimeError("AI 返回为空")
            except Exception as e:
                self._send(400, json.dumps({"error": f"提示词修改失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"prompt": content}, ensure_ascii=False))
            return
        if path == "/api/story_prompt":
            sb = body.get("storyboard") or {}
            roles = body.get("roles") or []
            token = _lm_token(body)
            if not sb.get("scene") and not sb.get("action"):
                self._send(400, json.dumps({"error": "分镜缺少场景或动作"}, ensure_ascii=False))
                return
            user = (
                "根据下面这个分镜生成分镜图提示词：\n"
                + json.dumps({"storyboard": sb, "roles": roles}, ensure_ascii=False, indent=1)
            )
            try:
                resp = lmstudio_chat([
                    {"role": "system", "content": STORY_PROMPT_SYSTEM},
                    {"role": "user", "content": user},
                ], token)
                content = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip().strip('"')
                if not content:
                    raise RuntimeError("AI 返回为空")
            except Exception as e:
                self._send(400, json.dumps({"error": f"提示词生成失败：{e}"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"prompt": content}, ensure_ascii=False))
            return
        if path == "/api/assemble":
            project_name = body.get("project_name", "")
            seg_range = body.get("seg_range")
            if seg_range and isinstance(seg_range, list) and len(seg_range) == 2:
                seg_range = (int(seg_range[0]) - 1, int(seg_range[1]))  # 前端 1 起 → 内部 0 起
            else:
                seg_range = None
            tid = start_assemble_job(project_name, body.get("selection") or {}, seg_range)
            self._send(200, json.dumps({"task_id": tid}, ensure_ascii=False))
            return
        if path == "/api/assemble_selected":
            """二次/多次合并：把选中的已合成视频按顺序拼接（用于批次结果合完整片）。"""
            files = body.get("files") or []
            if not isinstance(files, list) or not files:
                self._send(400, json.dumps({"error": "请先选择要合并的合成视频"}, ensure_ascii=False))
                return
            srcs = []
            for fn in files:
                fn = str(fn or "")
                if not fn.startswith("合成_") or not fn.lower().endswith(".mp4"):
                    self._send(400, json.dumps({"error": f"非法的合成文件名：{fn}"}, ensure_ascii=False))
                    return
                p = None
                for d in IMAGE_DIRS:
                    cand = os.path.join(d, fn)
                    if os.path.isfile(cand):
                        p = cand
                        break
                if not p:
                    self._send(404, json.dumps({"error": f"文件不存在：{fn}"}, ensure_ascii=False))
                    return
                srcs.append(p)
            st = load_state()
            proj = st.get("project") or {}
            base = _slug(proj.get("name") or "项目") or "项目"
            outname = f"合成_{base}_合并_{int(time.time())}.mp4"
            dest = os.path.join(IMAGE_DIRS[0], outname)
            tmpdir = tempfile.mkdtemp(prefix="merge_")
            try:
                segs = []
                for i, src in enumerate(srcs):
                    seg = os.path.join(tmpdir, f"seg_{i}.mp4")
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-i", src, "-c:v", "libx264", "-preset", "fast",
                         "-crf", "20", "-c:a", "aac", "-ar", "44100", "-pix_fmt", "yuv420p", seg],
                        capture_output=True,
                    )
                    if r.returncode != 0 or not os.path.exists(seg):
                        raise RuntimeError(f"转码失败：{src}")
                    segs.append(seg)
                listfile = os.path.join(tmpdir, "list.txt")
                with open(listfile, "w", encoding="utf-8") as f:
                    for p in segs:
                        f.write(f"file '{p.replace(os.sep, '/')}'\n")
                r = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", dest],
                    capture_output=True,
                )
                if r.returncode != 0 or not os.path.exists(dest):
                    raise RuntimeError(f"合并失败：{r.stderr.decode('utf-8', 'ignore')[-200:]}")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            self._send(200, json.dumps({"filename": outname}, ensure_ascii=False))
            return
        if path == "/api/export":
            tasks = body.get("tasks", [])
            csv_text = tasks_to_csv(tasks)
            self._send(200, json.dumps({"csv": csv_text}, ensure_ascii=False))
            return
        if path == "/api/enhance":
            prompt = body.get("prompt", "")
            task = body.get("task") or {}
            result = enhance_prompt(prompt, task)
            self._send(200, json.dumps({"prompt": result}, ensure_ascii=False))
            return
        if path == "/api/enhance_all":
            tasks = body.get("tasks", [])
            enhanced = [dict(t, prompt=enhance_prompt(t.get("prompt", ""), t)) for t in tasks]
            self._send(200, json.dumps({"tasks": enhanced}, ensure_ascii=False))
            return
        if path == "/api/clear_history":
            keep = body.get("keep") or None
            n = clear_history(keep)
            self._send(200, json.dumps({"cleared": True, "remaining": n}, ensure_ascii=False))
            return
        if path == "/api/delete_video":
            """删除某任务：视频文件（如有）一并删除，并移除任务记录（失败/丢失任务也可删）。"""
            task_id = str(body.get("task_id") or "")
            st = load_state()
            tasks = st.get("tasks", [])
            t = next((x for x in tasks if x.get("id") == task_id), None)
            if not t:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                return
            # 后端保护：任务还在远程生成/排队时禁止删除（前端按钮已隐藏，双保险）
            pid = t.get("prompt_id")
            if pid:
                try:
                    q = api_get(str(st.get("server") or DEFAULT_SERVER), "/queue")
                    active = {x[1] for x in q.get("queue_running", [])} | {x[1] for x in q.get("queue_pending", [])}
                    if pid in active:
                        self._send(400, json.dumps({"error": "任务正在生成/排队中，不能删除"}, ensure_ascii=False))
                        return
                except Exception:
                    pass  # 队列查不到时放行（保守不误伤已完成任务）
            of = t.get("output_file")
            if of:
                p = os.path.join(OUTPUTS_DIR, of.get("type", "output"), of.get("subfolder", ""), of.get("filename", ""))
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError as e:
                        self._send(400, json.dumps({"error": f"删除失败：{e}"}, ensure_ascii=False))
                        return
            st["tasks"] = [x for x in tasks if x.get("id") != task_id]
            save_state(st)
            self._send(200, json.dumps({"deleted": True, "task_id": task_id}, ensure_ascii=False))
            return
        if path == "/api/regenerate":
            """用原任务重新生成一版；body 可覆盖 prompt/quality/steps/mp。
            save_to_project=True 时同时把新提示词写回项目 prompt_tasks（永久）。"""
            task_id = str(body.get("task_id") or "")
            st = load_state()
            t = next((x for x in st.get("tasks", []) if x.get("id") == task_id), None)
            if not t:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                return
            new_task = {
                "id": t["id"] + "_r" + uuid.uuid4().hex[:6],
                "name": str(t.get("name") or t["id"]),
                "mode": t.get("mode"),
                "duration": t.get("duration"),
                "mp": body.get("mp", t.get("mp")),
                "prefix": t.get("prefix"),
                "image": t.get("image"),
                "images": t.get("images") or [],
                "story_image": t.get("story_image"),
                "ref_video": t.get("ref_video"),
                "prompt": str(body.get("prompt") or t.get("prompt") or "").strip(),
                "quality": body.get("quality", t.get("quality")),
                "steps": body.get("steps", t.get("steps")),
            }
            # 链式重生成：除第一段外，用上一段最新任务末帧接本段首帧
            if body.get("chain_mode"):
                pt_segs = (st.get("project") or {}).get("prompt_tasks") or []
                seg_idx = next(
                    (i for i, s in enumerate(pt_segs)
                     if str(s.get("name") or "") == str(t.get("name") or "")
                     or str(t.get("name") or "").startswith(str(s.get("name") or "") + "_")),
                    None,
                )
                if seg_idx is not None and seg_idx > 0:
                    prev_seg = pt_segs[seg_idx - 1]
                    pname = str(prev_seg.get("name") or "")
                    pc = [
                        x for x in st.get("tasks", [])
                        if (x.get("name") == pname or str(x.get("name") or "").startswith(pname + "_"))
                        and x.get("output_file") and x.get("downloaded")
                    ]
                    pc.sort(key=lambda x: str(x.get("submitted_at") or ""))
                    if pc:
                        new_task["chain_prev"] = pc[-1]["id"]
                        new_task["chain_waiting"] = True
                    else:
                        self._send(400, json.dumps({"error": f"上一段「{pname}」还没有视频，无法链式重生成"}, ensure_ascii=False))
                        return
            # 可选：把新提示词永久写回项目提示词库
            if body.get("save_to_project"):
                new_prompt = new_task["prompt"]
                st2 = load_state()
                proj2 = dict(st2.get("project") or {})
                pt2 = proj2.get("prompt_tasks") or []
                base_name = str(t.get("name") or "")
                hit = next(
                    (s for s in pt2
                     if str(s.get("name") or "") == base_name
                     or base_name.startswith(str(s.get("name") or "") + "_")),
                    None,
                )
                if hit and new_prompt:
                    hit["prompt"] = new_prompt
                    proj2["prompt_tasks"] = pt2
                    proj2["prompt_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    proj2["updated_at"] = proj2["prompt_updated_at"]
                    st2["project"] = proj2
                    projects2 = st2.get("projects") or {}
                    if proj2.get("name"):
                        projects2[proj2["name"]] = dict(proj2)
                        st2["projects"] = projects2
                    save_state(st2)
            server = st.get("server") or DEFAULT_SERVER
            results, err = submit_tasks(
                server, [new_task], auto_download=True,
                chain_mode=bool(body.get("chain_mode")),
            )
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"results": results}, ensure_ascii=False))
            return
        if path == "/api/delete_assembled":
            """删除合成视频（素材目录 合成_*.mp4）。"""
            filename = str(body.get("filename") or "")
            if not filename or not filename.startswith("合成_") or not filename.lower().endswith(".mp4"):
                self._send(400, json.dumps({"error": "非法的合成文件名"}, ensure_ascii=False))
                return
            deleted = False
            for d in IMAGE_DIRS:
                p = os.path.join(d, filename)
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                        deleted = True
                    except OSError as e:
                        self._send(400, json.dumps({"error": f"删除失败：{e}"}, ensure_ascii=False))
                        return
            if not deleted:
                self._send(404, json.dumps({"error": "文件不存在"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"deleted": True, "filename": filename}, ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ComfyUI 批量控制台已启动：http://127.0.0.1:{port}")
    print(f"工作流目录：{DEFAULT_WORKFLOW_DIR}")
    print(f"默认服务器：{DEFAULT_SERVER}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
