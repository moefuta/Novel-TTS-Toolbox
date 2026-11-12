# -*- coding: utf-8 -*-
"""
Novel TTS 服务 (tts.py) - 最终融合版
面向日文小说朗读的本地 TTS 聚合服务，兼容任意支持 OpenAI TTS 接口的阅读器
集成了：VOICEVOX、系统 SAPI、CeVIO AI (COM 情绪控制)、模型配置热重载
"""

import os
import re
import glob
import json
import base64
import asyncio
import threading
import tempfile
import uuid
import random
import time
import sys
import io
import wave
import webbrowser
import pathlib
import msvcrt
import edge_tts
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

import pythoncom
import win32com.client
import httpx

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

# ============================================================
# 一、路径与常量
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_DIR = os.path.join(BASE_DIR, "replace_rules")
AUTO_DICT_FILE = os.path.join(RULES_DIR, "auto_dict.txt")
AUTO_DICT_BASENAME = os.path.basename(AUTO_DICT_FILE)
RULES_CONFIG_FILE = os.path.join(BASE_DIR, "rules_enabled.json")
BG_DIR = os.path.join(BASE_DIR, "bg")
PERSON_DICT_FILE = os.path.join(RULES_DIR, "person_dict.txt")

MODELS_CONFIG_DIR = os.path.join(BASE_DIR, "model_configs")
DEFAULT_MODELS_FILE = os.path.join(MODELS_CONFIG_DIR, "default.json")
ACTIVE_MODELS_FILE = os.path.join(BASE_DIR, "model_config_active.json")
GLOBAL_CONFIG_FILE = os.path.join(BASE_DIR, "global_config.json")

VOICEVOX_URL = "http://127.0.0.1:50021"
VOICEVOX_HTTP_TIMEOUT = 60.0
VOICE_ORDER = ["alloy", "echo", "fable", "nova", "onyx", "shimmer"]

_DEFAULT_SAPI = {
    "satou":   "CeVIO-AI-さとうささら", "rikka":   "CeVIO-AI-小春六花",
    "maki":    "CeVIO-AI-弦巻マキ", "tsudumi": "CeVIO-AI-すずきつづみ",
    "karin":   "CeVIO-AI-夏色花梨", "rosa":    "CeVIO-AI-ROSA",
    "chifuyu": "CeVIO-AI-花隈千冬", "one":     "CeVIO-AI-ONE",
}

# ============================================================
# 文本分块 / WAV 拼接（长文本分批提交，避免引擎端长时间阻塞）
# ============================================================
# 单批最大字符数。日文小说里 120 字约等于 2~6 个逗号分句：
#   - 太大会导致 TTS引擎 的 /audio_query 对长句解析变慢（超线性）
#   - 太小则 HTTP / COM 调用次数过多，开销反超
# 如果仍然觉得首音延迟明显，可继续下调到 80；想减少请求数可上调到 200。
TTS_BATCH_MAX_CHARS = 120


def _split_text_for_batch(text: str, max_chars: int = TTS_BATCH_MAX_CHARS) -> list[str]:
    """
    按标点边界把长文本拆成多批，每批长度尽量接近但不超过 max_chars。
    拆分规则：
      1. 优先在强标点后切：。！？!?；; 换行
      2. 单句仍超长时，再在弱标点后切：，,、空白
      3. 若一段完全无标点且超长，则整段作为一批（宁可慢也不切断 ruby 结构）
    由于 ruby 结构（｜汉字《假名》）内部不包含任何标点，
    此方法不会把一条 ruby 注音切成两半。

    **无论文本总长是否超过 max_chars，都会先按标点切分再拼批**。
    这样即使是一段 100 多字的单个长句（只有一个句号、内部有逗号），
    也会在逗号处被切开，避免整段长句一次性丢给 TTS引擎 导致解析卡顿。

    拆分规则：
      1. 强标点切句：。！？!?；; 换行
      2. 弱标点切分句：，,、空白（一定会执行，不再看长度）
      3. 贪心拼批：尽量填满 max_chars，但绝不把一个分句切成两半
      4. 若某分句本身超长（无标点长句），则整段作为一批——
         宁可慢，也不切断 ruby 结构（｜汉字《假名》内部不含标点，天然安全）
    """
    text = (text or "").strip()
    if not text:
        return []

    # 第一层：按强标点切句
    sentences = re.split(r'(?<=[。！？!?；;\n])', text)
    sentences = [s for s in sentences if s]

    # 第二层：无条件按弱标点切分句（保留标点本身，lookbehind 不消耗字符）
    units = []
    for s in sentences:
        units.extend(re.split(r'(?<=[，,、\s])', s))
    units = [u for u in units if u]

    # 第三层：贪心拼批
    batches = []
    cur = ""
    for u in units:
        if len(cur) + len(u) <= max_chars:
            cur += u
        else:
            if cur:
                batches.append(cur)
                cur = ""
            if len(u) > max_chars:
                # 无法再拆的超长无标点段，原样作为一批
                batches.append(u)
            else:
                cur = u
    if cur:
        batches.append(cur)
    return batches


def _concat_wav_bytes(wav_list: list[bytes]) -> bytes:
    """把多段完整 wav 字节拼接为一段 wav 字节（要求格式一致）。"""
    if not wav_list:
        return b""
    if len(wav_list) == 1:
        return wav_list[0]
    with wave.open(io.BytesIO(wav_list[0]), "rb") as w0:
        nch, sw, fr = w0.getnchannels(), w0.getsampwidth(), w0.getframerate()
    frames_all = bytearray()
    for wb in wav_list:
        with wave.open(io.BytesIO(wb), "rb") as wf:
            if wf.getnchannels() != nch or wf.getsampwidth() != sw or wf.getframerate() != fr:
                raise RuntimeError("WAV 格式不一致，无法拼接")
            frames_all.extend(wf.readframes(wf.getnframes()))
    out = io.BytesIO()
    with wave.open(out, "wb") as wo:
        wo.setnchannels(nch)
        wo.setsampwidth(sw)
        wo.setframerate(fr)
        wo.writeframes(bytes(frames_all))
    return out.getvalue()


def _concat_wav_files(src_paths: list[str], dst_path: str) -> None:
    """把多个 wav 文件合并写到 dst_path（要求格式一致）。"""
    if not src_paths:
        raise RuntimeError("没有可合并的音频块")
    if len(src_paths) == 1:
        os.replace(src_paths[0], dst_path)
        return
    with wave.open(src_paths[0], "rb") as w0:
        nch, sw, fr = w0.getnchannels(), w0.getsampwidth(), w0.getframerate()
    with wave.open(dst_path, "wb") as wo:
        wo.setnchannels(nch)
        wo.setsampwidth(sw)
        wo.setframerate(fr)
        for p in src_paths:
            with wave.open(p, "rb") as wf:
                if wf.getnchannels() != nch or wf.getsampwidth() != sw or wf.getframerate() != fr:
                    raise RuntimeError(f"音频格式不一致: {p}")
                wo.writeframes(wf.readframes(wf.getnframes()))


MODEL_SPEAKERS = {}  # 废弃旧版映射，现已全面由 _get_entity_model_names 动态生成 1~999
SPECIAL_MODELS = dict(_DEFAULT_SAPI)
CEVIO_COM_PRESETS = {}  # 专门存情绪预设

def _get_entity_model_names() -> list[str]:
    # 直接返回 1~999 的字符串作为 VOICEVOX 模型列表
    voicevox_ids = [str(i) for i in range(1, 999)]
    return voicevox_ids + list(SPECIAL_MODELS.keys()) + list(CEVIO_COM_PRESETS.keys())

def _get_model_names() -> list[str]:
    return _get_entity_model_names() + ["auto"]

# ============================================================
# 二、全局状态
# ============================================================
_system_voices = []
_voices_lock = threading.Lock()
_sapi_lock = threading.Lock()
_com_lock = threading.Lock()   # 用于 CeVIO COM 线程锁

_all_rules = []
_rules_dir_state = None
_auto_dict_keys = set()
_person_names = set()  # 存放人名列表用于助词修正

# 人名后缀读音映射
# 结构：{原字: {"type": "ruby"|"kana", "reading": "读音"}}
# - "ruby" 型：汉字后缀，生成 ｜原字《读音》 注音框（如 君 -> くん、家 -> け）
# - "kana" 型：假名后缀（助词正字法等），直接替换为读音（如 は -> わ）
# 内置默认值，可由 person_dict.txt 中的 [suffix] 行扩展/覆盖
_DEFAULT_PERSON_SUFFIXES = {
    "家": {"type": "ruby", "reading": "け"},
    "は": {"type": "kana", "reading": "わ"},
}
_person_suffixes = dict(_DEFAULT_PERSON_SUFFIXES)

_exact_dict_lookup = {}  # ★ 存放纯文本规则，用于优先拦截
_auto_dict_words = {}  # ★ 存放仅作登记的 auto_dict 词条
_auto_dict_lock = threading.Lock()
_dict_lock = threading.RLock()

_rules_enabled_files = None

_http_client: httpx.AsyncClient | None = None
_voicevox_ready = False

_monitor_records: deque = deque(maxlen=100)
_monitor_lock = threading.Lock()

_auto_config = {"model": "karin", "voice": "alloy", "speed": 0.9}
_auto_config_lock = threading.Lock()

_models_config_lock = threading.RLock()
_active_models_filename = "default.json"

_global_config = {"output_format": "C"}  # 默认使用 Ruby 输出
_global_config_lock = threading.Lock()

# 记录前端最后一次请求的时间，用于判断网页是否已打开
_last_frontend_ping = 0.0

# 快捷键重启相关状态
_restart_requested = False
_server_instance = None
_browser_opened = False   # 防止重启服务时重复弹出浏览器

def _load_global_config():
    global _global_config
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                _global_config.update(json.load(f))
        except Exception:
            pass

def _save_global_config():
    with _global_config_lock:
        with open(GLOBAL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_global_config, f, ensure_ascii=False, indent=2)

# ============================================================
# 三、CeVIO COM 情绪合成核心函数
# ============================================================
def _get_cevio_talker():
    try:
        return win32com.client.Dispatch("CeVIO.Talk.RemoteService2.Talker2V40")
    except Exception as e:
        raise RuntimeError(f"无法连接 CeVIO AI，请确保程序已启动。详情：{e}")

def _synthesize_com_sync(text: str, cast: str, emotions: dict, speed: int, tone_scale: int, out_path: str):
    """
    按批次提交 CeVIO COM，避免单次 OutputWaveToFile 因文本过长而失败或阻塞。
    COM 调用在 _com_lock 内串行完成，WAV 拼接在锁外完成，不阻塞其它 COM 请求。
    """
    batches = _split_text_for_batch(text, TTS_BATCH_MAX_CHARS)
    if not batches:
        raise RuntimeError("朗读文本为空")

    tmp_paths: list[str] = []
    try:
        with _com_lock:
            pythoncom.CoInitialize()
            try:
                talker = _get_cevio_talker()
                talker.Cast = cast
                talker.Speed = max(0, min(100, int(speed)))
                talker.ToneScale = max(0, min(100, int(tone_scale)))
                components = talker.Components
                index_by_name = {}
                for i in range(components.Length):
                    comp = components.At(i)
                    index_by_name[comp.Name.lower()] = i
                for name, val in emotions.items():
                    key = name.lower()
                    if key in index_by_name:
                        idx = index_by_name[key]
                        components.At(idx).Value = max(0, min(100, int(val)))

                for i, batch in enumerate(batches):
                    tmp_path = out_path + f".part{i}.wav"
                    tmp_paths.append(tmp_path)
                    ok = talker.OutputWaveToFile(batch, tmp_path)
                    if not ok or not os.path.exists(tmp_path):
                        raise RuntimeError(
                            f"OutputWaveToFile 失败（第 {i+1}/{len(batches)} 批）"
                        )
            finally:
                pythoncom.CoUninitialize()

        # 合并（在锁外）
        _concat_wav_files(tmp_paths, out_path)
    finally:
        for p in tmp_paths:
            try:
                if os.path.exists(p) and p != out_path:
                    os.remove(p)
            except Exception:
                pass

def _get_casts_sync():
    with _com_lock:
        pythoncom.CoInitialize()
        try:
            talker = _get_cevio_talker()
            casts = talker.AvailableCasts
            return [casts.At(i) for i in range(casts.Length)]
        finally:
            pythoncom.CoUninitialize()

def _get_emotions_sync(cast: str):
    with _com_lock:
        pythoncom.CoInitialize()
        try:
            talker = _get_cevio_talker()
            talker.Cast = cast
            components = talker.Components
            return [{"name": components.At(i).Name, "default": components.At(i).Value}
                    for i in range(components.Length)]
        finally:
            pythoncom.CoUninitialize()

# ============================================================
# 四、模型配置文件读写
# ============================================================
def _normalize_voicevox_map(raw: dict) -> dict:
    if not isinstance(raw, dict): raise ValueError("voicevox 必须是对象")
    out = {}
    for name, ids in raw.items():
        if not isinstance(name, str) or not name.strip(): raise ValueError(f"voicevox.model 名无效")
        if not isinstance(ids, list) or not ids: raise ValueError(f"voicevox.{name} 必须是非空数组")
        out[name.strip()] = [int(x) for x in ids]
    return out

def _normalize_sapi_map(raw: dict) -> dict:
    if not isinstance(raw, dict): raise ValueError("sapi 必须是对象")
    return {k.strip(): v.strip() for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

def _apply_models_config(voicevox_map: dict, sapi_map: dict, com_map: dict):
    with _models_config_lock:
        MODEL_SPEAKERS.clear(); MODEL_SPEAKERS.update(voicevox_map)
        SPECIAL_MODELS.clear(); SPECIAL_MODELS.update(sapi_map)
        CEVIO_COM_PRESETS.clear(); CEVIO_COM_PRESETS.update(com_map)
    _validate_auto_config()

def _validate_auto_config():
    names = _get_entity_model_names()
    if not names: return
    with _auto_config_lock:
        if _auto_config["model"] not in names:
            _auto_config["model"] = names[0]
            print(f"[auto] 原配置失效，重置为 {names[0]}")

def _read_models_json(path: str):
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    vv = _normalize_voicevox_map(data.get("voicevox", {}))
    sp = _normalize_sapi_map(data.get("sapi", {}))
    com = data.get("cevio_com", {})
    return vv, sp, com

def _write_models_json(path: str, vv_map: dict, sp_map: dict, com_map: dict):
    payload = {"voicevox": vv_map, "sapi": sp_map, "cevio_com": com_map}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _save_active_filename(filename: str):
    global _active_models_filename
    _active_models_filename = filename
    with open(ACTIVE_MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump({"active_file": filename}, f, ensure_ascii=False, indent=2)

def _init_models_config():
    os.makedirs(MODELS_CONFIG_DIR, exist_ok=True)
    if not os.path.exists(DEFAULT_MODELS_FILE):
        _write_models_json(DEFAULT_MODELS_FILE, _DEFAULT_VOICEVOX, _DEFAULT_SAPI, {})

    active_name = "default.json"
    try:
        with open(ACTIVE_MODELS_FILE, "r", encoding="utf-8") as f:
            name = json.load(f).get("active_file")
            if name and os.path.exists(os.path.join(MODELS_CONFIG_DIR, name)):
                active_name = name
    except Exception: pass

    path = os.path.join(MODELS_CONFIG_DIR, active_name)
    try:
        vv, sp, com = _read_models_json(path)
        _apply_models_config(vv, sp, com)
        _save_active_filename(active_name)
        print(f"[模型配置] 已加载 {active_name}")
    except Exception as e:
        print(f"[模型配置] 加载失败 {e}，使用默认")
        _apply_models_config(dict(_DEFAULT_VOICEVOX), dict(_DEFAULT_SAPI), {})

    # ★ 新增逻辑：如果存在 my_config.json，自动将其第一个 cevio_com 预设设为 auto 默认模型
    my_config_path = os.path.join(MODELS_CONFIG_DIR, "my_config.json")
    if os.path.exists(my_config_path):
        try:
            with open(my_config_path, "r", encoding="utf-8-sig") as f:
                my_data = json.load(f)
            my_com = my_data.get("cevio_com", {})
            if my_com:
                first_model = list(my_com.keys())[0]
                with _auto_config_lock:
                    _auto_config["model"] = first_model
                print(f"[auto] 检测到 my_config.json，已将默认模型切换为: {first_model}")
        except Exception as e:
            print(f"[auto] 读取 my_config.json 失败: {e}")

def _log_sapi_models_status():
    if not _system_voices: return
    for m, kw in SPECIAL_MODELS.items():
        hit = next((d for d in _system_voices if kw.lower() in d.lower()), None)
        if hit: print(f"[语音] SAPI 模型 '{m}' 可用 -> {hit}")

# ============================================================
# 五、规则加载 (与原版一致)
# ============================================================
_COUNT_SUFFIX_RE = re.compile(r'^(.*?)\s*<\s*=\s*count\s*:\s*(\d+)\s*$', re.IGNORECASE)
_REGEX_META_CHARS = set('.^$*+?{}[]\\|()')
_LATIN_BOUNDARY = r"A-Za-z0-9ａ-ｚＡ-Ｚ０-９"

def _is_plain_text(s: str) -> bool:
    if not s: return False
    return not any(ch in _REGEX_META_CHARS for ch in s)

def _build_latin_fold_pattern(s: str) -> str:
    """
    把纯文本中的拉丁字母、ASCII 数字、半/全角空格，
    转为对「大小写 + 全角半角」都兼容的正则字符类；其余字符按字面量 re.escape。
    
    ★ 新特性：自动允许字符之间出现任意个半角或全角空格，防止 EPUB 排版空格导致规则失效。
    """
    parts = []
    chars = list(s)
    for i, ch in enumerate(chars):
        o = ord(ch)
        if 0x41 <= o <= 0x5A:              # A-Z
            lo = chr(o + 0x20)
            up = ch
            fw_lo = chr(o - 0x41 + 0xFF41)   # ａ-ｚ
            fw_up = chr(o - 0x41 + 0xFF21)   # Ａ-Ｚ
            parts.append('[' + re.escape(lo) + re.escape(up) + re.escape(fw_lo) + re.escape(fw_up) + ']')
        elif 0x61 <= o <= 0x7A:            # a-z
            lo = ch
            up = chr(o - 0x20)
            fw_lo = chr(o - 0x61 + 0xFF41)
            fw_up = chr(o - 0x61 + 0xFF21)
            parts.append('[' + re.escape(lo) + re.escape(up) + re.escape(fw_lo) + re.escape(fw_up) + ']')
        elif 0x30 <= o <= 0x39:            # 0-9
            fw = chr(o - 0x30 + 0xFF10)    # ０-９
            parts.append('[' + re.escape(ch) + re.escape(fw) + ']')
        elif ch == ' ' or ch == '\u3000':  # 半角/全角空格
            parts.append(r'[ \u3000]*')
        else:
            parts.append(re.escape(ch))

        # ★ 如果不是最后一个字符，且当前字符不是空格，允许中间出现任意个空格
        if i < len(chars) - 1 and ch not in ' \u3000':
            parts.append(r'[ \u3000]*')

    body = ''.join(parts).replace(r'[ \u3000]*[ \u3000]*', r'[ \u3000]*')

    if s and all(ch.isascii() and ch.isalnum() for ch in s):
        return (
            rf'(?<![{_LATIN_BOUNDARY}])'
            + body
            + rf'(?![{_LATIN_BOUNDARY}])'
        )
    return body

def _parse_count_suffix(text: str):
    """
    解析 `xxx <= count:N` 后缀。
    返回的 count 是**优先级权重**（不是替换次数，是来自epub文件的命中次数）：
      - 默认 1
      - 同一个 pattern 在多个规则文件中冲突时，N 越大越优先（无视文件名字母序）
      - 规则一旦生效会替换全文所有匹配，与 count 无关
    """
    m = _COUNT_SUFFIX_RE.match(text)
    if m:
        try: return m.group(1).strip(), max(1, int(m.group(2)))
        except: pass
    return text.strip(), 1

def _split_equivalent_patterns(pattern_str):
    if '|' not in pattern_str and '｜' not in pattern_str: return [pattern_str]
    parts = [p.strip() for p in re.split(r'[|｜]', pattern_str) if p.strip()]
    if not parts or not all(_is_plain_text(p) for p in parts): return [pattern_str]
    return parts

def _init_rules_dir():
    # 1. 检查并创建必需的目录
    if not os.path.exists(RULES_DIR): 
        os.makedirs(RULES_DIR)
    if not os.path.exists(BG_DIR):
        os.makedirs(BG_DIR)
        
    # 2. 检查并创建 auto_dict.txt
    if not os.path.exists(AUTO_DICT_FILE):
        with open(AUTO_DICT_FILE, "w", encoding="utf-8") as f:
            f.write("# ==========================================\n")
            f.write("# 自动记忆词典（auto_dict.txt）\n")
            f.write("# ==========================================\n")
            f.write("# 当原文（如 EPUB）中出现了规则库未收录的注音时，\n")
            f.write("# 系统会自动将其登记在这里。\n#\n")
            f.write("# ⚠️ 注意：此文件内的规则【不会直接参与替换】，\n")
            f.write("# 仅作为人工确认的素材，防止机器错标污染全局词库。\n")
            f.write("# 确认读音无误后，请手动将其复制到 user_dict.txt 或其他规则文件中。\n\n")
            
    # 3. 检查并创建示例 user_dict.txt
    user_dict_path = os.path.join(RULES_DIR, "user_dict.txt")
    if not os.path.exists(user_dict_path):
        with open(user_dict_path, "w", encoding="utf-8") as f:
            f.write("# 这是一个示例自定义词典（user_dict.txt）\n")
            f.write("# 所有自定义的「原文 => 读音」规则都可以写在这里。保存后无需重启，下一次朗读自动热重载生效。\n\n")
            f.write("# 示例\n")
            f.write("# input= '今日は、昨日よりもっと沢山の漢字が読めるようになった。'\n")
            f.write("# output= '｜今日《きょう》は、｜昨日《きのう》よりもっと沢山の｜漢字《かんじ》が読めるようになった。'\n\n")
            f.write("漢字 => かんじ\n")
            f.write("昨日 => きのう\n")
            f.write("今日 => きょう\n\n")
            f.write("# ----- 进阶语法说明（如果不需要可以删除） -----\n")
            f.write("# 1. 多读音概率（按比例随机分配）：\n")
            f.write("# 昨日 => きのう(70%)|さくじつ(30%)\n\n")
            f.write("# 2. 优先级权重（数值越大，越能覆盖其他词典里的同词规则）：\n")
            f.write("# 今日 => きょう <= count:100\n")

    # 4. 检查并创建示例 person_dict.txt
    if not os.path.exists(PERSON_DICT_FILE):
        with open(PERSON_DICT_FILE, "w", encoding="utf-8") as f:
            f.write("# 这是一个示例自定义词典（person_dict.txt）\n")
            f.write("# 所有自定义的「(A|B|C) => (a|b|c)」规则都可以写在这里。保存后无需重启，下一次朗读自动热重载生效。\n\n")
            f.write("# 示例\n")
            f.write("# input= '桐ヶ谷君は、何時、我が娘、結城明日奈を、桐ヶ谷明日奈に仕立てるつもり？'\n")
            f.write("# output= '|桐ヶ谷《きりがや》|君《くん》わ、何時、我が娘、|結城《ゆうき》|明日奈《あすな》を、|桐ヶ谷《きりがや》|明日奈《あすな》に仕立てるつもり？'\n\n")
            f.write("# 专属规则\n")
            f.write("[suffix] 君 => くん\n")
            f.write("[suffix] 様 => さま\n")
            f.write("[suffix] 殿 => どの\n")
            f.write("[suffix] 家 => け\n")
            f.write("[suffix] は => わ\n\n")
            f.write("(結城|明日奈|アスナ|asuna) => (ゆうき|あすな|アスナ|アスナ)\n")
            f.write("(桐ヶ谷|和人|キリト|kirito) => (きりがや|かずと|キリト|キリト)\n\n")
            f.write("# ----- 进阶语法说明（如果不需要可以删除） -----\n")
            f.write("# 问：如果有一些本不该被念成'わ'的'は'，但是TTS引擎始终教不会，怎么办？\n")
            f.write("# 答：使用片假名，示例如下\n\n")
            f.write("(桐ヶ谷|直葉|リーファ|すぐは) => (きりがや|スグハ|リーファ|スグハ)\n")

def _load_rules_config():
    global _rules_enabled_files
    try:
        with open(RULES_CONFIG_FILE, "r", encoding="utf-8") as f:
            _rules_enabled_files = set(json.load(f).get("enabled_files", []))
    except Exception: _rules_enabled_files = None

def _save_rules_config(enabled_files):
    global _rules_enabled_files
    with open(RULES_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"enabled_files": sorted(enabled_files)}, f, ensure_ascii=False, indent=2)
    _rules_enabled_files = set(enabled_files)
    return True

def _is_file_enabled(filename: str) -> bool:
    if filename == AUTO_DICT_BASENAME: return True
    return True if _rules_enabled_files is None else filename in _rules_enabled_files

_WEIGHT_RE = re.compile(r'^(.+?)[（(]\s*(\d*\.?\d+)\s*([%％]?)\s*[)）]$')

def _parse_replacement_options(text):
    parts = [p.strip() for p in text.split('|')]
    if len(parts) <= 1: return [(parts[0], 1.0)]
    matched = [_WEIGHT_RE.match(p) for p in parts]
    has_weight = any(m is not None for m in matched)
    if has_weight and not all(m is not None for m in matched): return [(text, 1.0)]
    if not has_weight: return [(p, 1.0) for p in parts]
    return [(m.group(1).strip(), max(0.0, float(m.group(2)))) for m in matched]

def _parse_single_file(path):
    rules, errors, person_names, suffixes = [], [], set(), {}
    filename = os.path.basename(path)
    
    # ★ 判断是否为专用的人名词典文件（严格绑定 person_dict.txt）
    is_person_file = (filename == "person_dict.txt")

    try:
        with open(path, "r", encoding="utf-8-sig") as f: lines = f.readlines()
    except Exception as e: return None, [f"{filename} 读取失败：{e}"], set(), {}

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"): continue
        line = line.replace("＝＞", "=>").replace("=》", "=>").replace("→", "=>").replace("《=", "<=").replace("←", "<=")
        if "=>" not in line: continue
        
        # ★ 新增：解析 [suffix] 后缀读音规则（仅 person_dict.txt 中生效）
        # 语法：[suffix] 原字 => 读音
        # - 原字为汉字：生成 ｜原字《读音》 注音框（ruby 型）
        # - 原字为假名：直接替换为读音（kana 型，用于助词正字法）
        if is_person_file:
            m_suffix = re.match(r'^\[suffix\]\s*(.+?)\s*=>\s*(.+)$', line)
            if m_suffix:
                orig_char = m_suffix.group(1).strip()
                reading_part = m_suffix.group(2).strip()
                reading, _ = _parse_count_suffix(reading_part)
                if orig_char and reading:
                    is_kana_char = bool(re.fullmatch(r'[ぁ-んァ-ヶー]+', orig_char))
                    suffixes[orig_char] = {
                        "type": "kana" if is_kana_char else "ruby",
                        "reading": reading,
                    }
                continue
        
        # ★ 非 person_dict.txt 文件中的 [suffix] 行直接忽略，避免误解析为正则
        if line.startswith("[suffix]"):
            continue
        
        # ★ 只有在文件名符合要求时，才解析 (苗字|名前|别名) => (...) 语法
        if is_person_file:
            # 1. 先分离左右两边
            left_side, right_side = line.split("=>", 1)
            
            # 2. 剥离右侧的 count 后缀
            replacement, count = _parse_count_suffix(right_side)
            
            # 3. 严格匹配左括号 和 剥离后的右括号
            m_person_left = re.match(r'^\((.*?)\)\s*$', left_side.strip())
            if m_person_left and replacement.startswith('(') and replacement.endswith(')'):
                left_parts = [p.strip() for p in m_person_left.group(1).split('|')]
                right_parts = [p.strip() for p in replacement.strip('()').split('|')]
                
                if len(left_parts) == len(right_parts) and all(left_parts):
                    for k, v in zip(left_parts, right_parts):
                        person_names.add(k)
                        # 如果原词与读音完全一致（比如别名アスナ），不生成替换规则，只作为人名登记
                        if k != v and v:
                            options = _parse_replacement_options(v)
                            compiled = re.compile(_build_latin_fold_pattern(k)) if _is_plain_text(k) else re.compile(k)
                            # ★ 加上原词 k
                            rules.append((compiled, options, count, len(k), k))
                    continue
                
        left, right = line.split("=>", 1)
        pattern_str, replacement, count = left.strip(), *_parse_count_suffix(right)
        if not pattern_str: continue
        options = _parse_replacement_options(replacement)
        for part in _split_equivalent_patterns(pattern_str):
            try:
                compiled = re.compile(_build_latin_fold_pattern(part)) if _is_plain_text(part) else re.compile(part)
            except Exception:
                compiled = re.compile(re.escape(part))
            # ★ 加上原词 part
            rules.append((compiled, options, count, len(part), part))
    return rules, errors, person_names, suffixes

def load_all_rules(force=False):
    global _all_rules, _rules_dir_state, _auto_dict_keys, _person_names, _exact_dict_lookup, _person_suffixes
    txt_files = sorted(glob.glob(os.path.join(RULES_DIR, "*.txt")))
    file_stats = tuple((f, os.stat(f).st_mtime, os.stat(f).st_size) for f in txt_files if os.path.exists(f))
    enabled_names = tuple(sorted(os.path.basename(f) for f in txt_files if _is_file_enabled(os.path.basename(f))))
    current_state = (enabled_names, file_stats)
    
    with _dict_lock:
        if not force and current_state == _rules_dir_state: 
            return
            
    auto_keys, pattern_map = set(), {}
    new_person_names = set()
    new_exact_dict = {}
    new_person_suffixes = dict(_DEFAULT_PERSON_SUFFIXES)  # ★ 内置默认后缀
    
    new_auto_dict_words = {}
    for filepath in txt_files:
        filename = os.path.basename(filepath)
        if not _is_file_enabled(filename): 
            continue
            
        is_auto = (filename == AUTO_DICT_BASENAME)
        rules, _, persons, suffixes = _parse_single_file(filepath)
        if rules is None: 
            continue
            
        if is_auto:
            # ★ auto_dict 仅作登记，不加入生效规则池
            for compiled, options, count, orig_len, original_text in rules:
                if options:
                    new_auto_dict_words[original_text] = options[0][0]
            continue
            
        new_person_names.update(persons)
        new_person_suffixes.update(suffixes)  # ★ 收集外部后缀规则（覆盖默认）
        
        # ★ 注意这里的解包多了一个 original_text
        for compiled, options, count, orig_len, original_text in rules:
            if is_auto: 
                auto_keys.add(compiled.pattern)
                
            # ★ 核心修改：优先判断 person，保证其映射绝对不会被其他文件覆盖
            if is_auto:
                priority = (2, -count)
            elif filename == "person_dict.txt":
                priority = (0, -count)  # 0 最小，代表最高优先级
            else:
                priority = (1, -count)  # 1 代表普通规则
            if compiled.pattern not in pattern_map or priority < pattern_map[compiled.pattern][0]:
                pattern_map[compiled.pattern] = (priority, compiled, options, orig_len)
                
            # ★ 记录纯文本的精确映射，用于阻断错误的 EPUB 原文注音
            if _is_plain_text(original_text):
                new_exact_dict[original_text] = options
                # 额外生成一个不含空格的 Key，用于兼容带空格的原词（如 終　章）
                no_space_text = re.sub(r'[ \u3000]', '', original_text)
                if no_space_text != original_text:
                    new_exact_dict[no_space_text] = options
                
    all_loaded_rules = [(v[1], v[2], v[3]) for v in pattern_map.values()]
    all_loaded_rules.sort(key=lambda x: x[2], reverse=True)
    
    with _dict_lock:
        _all_rules = all_loaded_rules
        _auto_dict_keys = auto_keys
        _rules_dir_state = current_state
        _person_names = new_person_names
        _person_suffixes = new_person_suffixes  # ★ 更新后缀映射
        _exact_dict_lookup = new_exact_dict

    with _auto_dict_lock:
        _auto_dict_words.clear()
        _auto_dict_words.update(new_auto_dict_words)

def apply_replace_rules(text, output_mode='B', clean_punct=True):
    load_all_rules()
    with _dict_lock: rules_snapshot = list(_all_rules)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", text)
    
    ruby_protect = []
    
    # ★ 核心保护函数：将已经生成的 ｜...《...》 替换为安全的占位符
    def protect_rubies(current_text):
        def repl(m):
            ruby_protect.append(m.group(0))
            return f"\x00RUBY{len(ruby_protect)-1}\x00"
        return re.sub(r'[｜|][^《]*《[^》]*》', repl, current_text)

    # 保护传入时就已有的 Ruby
    if output_mode == 'C':
        text = protect_rubies(text)

    for pattern, options, _ in rules_snapshot:
        def replacer(m, options=options, output_mode=output_mode):
            original = m.group(0)
            if len(options) == 1:
                chosen = options[0][0]
            else:
                texts = [o[0] for o in options]
                weights = [o[1] for o in options]
                chosen = random.choices(texts, weights=weights, k=1)[0]
                
            try: chosen_expanded = m.expand(chosen)
            except Exception: chosen_expanded = chosen
                
            if output_mode == 'A': return original
            elif output_mode == 'B': return chosen_expanded
            elif output_mode == 'C': 
                # ★ 核心修复：只有当原词是纯数字（如 "24"）时，才直接输出替换结果（如 "にじゅう4"），不加注音框
                # 这样就可以让后续的 "4本" 正常命中并打上注音框，同时保证 "現実世界" 等正常词汇打上 Ruby 框
                if re.match(r'^[0-9０-９]+$', original):
                    return chosen_expanded
                
                clean_ruby = re.sub(r'[·・\s]', '', chosen_expanded)
                return f"｜{original}《{clean_ruby}》"

        try:
            text = pattern.sub(replacer, text)
            # ★ 关键修复：每执行完一条长词替换，立刻保护它不被后续的短词吃掉
            if output_mode == 'C':
                text = protect_rubies(text)
        except Exception: 
            pass
            
    # 彻底还原所有保护
    if output_mode == 'C':
        for i, p in enumerate(ruby_protect):
            text = text.replace(f"\x00RUBY{i}\x00", p)
        
    # 清理括号
    if clean_punct:
        if output_mode != 'C':
            text = re.sub(r"[『』【】《》〈〉〔〕［］｛｝「」]", "", text)
        else:
            text = re.sub(r"[『』【】〈〉〔〕［］｛｝「」]", "", text)
        
    return re.sub(r"^[、，,]+|[、，,]+$", "", re.sub(r"([、，,])[、，,]+", r"\1", re.sub(r"[・]", "、", text)))


def fix_person_suffixes(text, output_mode='C'):
    """
    自动修正人名后面的称呼/助词后缀读音。
    后缀映射表由 person_dict.txt 中的 [suffix] 行定义（内置默认：家→け、は→わ）。
    """
    with _dict_lock:
        if not _person_names: return text
        names_snapshot = list(_person_names)
        suffix_map = dict(_person_suffixes)
        
    if not names_snapshot or not suffix_map: return text
    sorted_names = sorted(names_snapshot, key=len, reverse=True)
    name_group = '(' + '|'.join(re.escape(n) for n in sorted_names) + ')'
    
    # 从 suffix_map 的键构建字符类（支持单字符后缀）
    suffix_chars = ''.join(re.escape(ch) for ch in suffix_map.keys())
    
    # 匹配：人名 + 可选的《注音》 + 连续的后缀字符
    pattern = re.compile(name_group + r'(?:《[^》]*》)?([' + suffix_chars + r']+)')
    
    def replacer(m):
        # 获取到后缀开始前的字符（包含人名和可能有的注音）
        name_part = m.group(0)[:m.start(2) - m.start(0)]
        suffix_str = m.group(2)
        
        res = ""
        for ch in suffix_str:
            info = suffix_map.get(ch)
            if info is None:
                res += ch
                continue
            if info["type"] == "ruby":
                if output_mode == 'C':
                    res += f'｜{ch}《{info["reading"]}》'
                elif output_mode == 'B':
                    res += info["reading"]
                else:
                    res += ch
            else:  # kana 型：直接替换（助词正字法等）
                res += info["reading"]
                
        return name_part + res
        
    return pattern.sub(replacer, text)

# ============================================================
# 六、Ruby 解析 (与原版一致)
# ============================================================
RUBY_KANA = r"[ぁ-んァ-ヶーヽヾゝゞ]+"
RUBY_PATTERNS = [
    (re.compile(r"<ruby>\s*([^<]+?)\s*(?:<rp>[^<]*</rp>)?\s*<rt>\s*([^<]+?)\s*</rt>\s*(?:<rp>[^<]*</rp>)?\s*</ruby>", re.IGNORECASE | re.DOTALL), lambda m: (m.group(1).strip(), m.group(2).strip())),
    (re.compile(r"[｜|]([^《》｜|]+)《([^《》]+)》"), lambda m: (m.group(1).strip(), m.group(2).strip())),
    (re.compile(r"([一-龥々〆ヵヶ]+)《(" + RUBY_KANA + r")》"), lambda m: (m.group(1), m.group(2))),
    (re.compile(r"[{｛]([^{}｛｝|｜]+)[|｜]([^{}｛｝|｜]+)[}｝]"), lambda m: (m.group(1).strip(), m.group(2).strip())),
    (re.compile(r"([一-龥々〆ヵヶ]+)［(" + RUBY_KANA + r")］"), lambda m: (m.group(1), m.group(2))),
    (re.compile(r"([一-龥々〆ヵヶ]+)\[(" + RUBY_KANA + r")\]"), lambda m: (m.group(1), m.group(2))),
]

def _merge_curly_braces(match_str):
    pairs = re.findall(r'[{｛]([^{}｛｝|｜]+)[|｜]([^{}｛｝|｜]+)[}｝]', match_str)
    bases = ''.join(p[0] for p in pairs)
    rubies = ''.join(p[1] for p in pairs)
    return f"{{{bases}|{rubies}}}"

def _merge_adjacent_rubies(text):
    """将相邻的多个 Ruby 标签合并，并尝试用词典中的词组进行替换（丢弃原有注音）"""
    if not text: return text

    # 1. 针对 HTML ruby 的连续标签
    html_ruby_block = r'(?:<ruby\b[^>]*>\s*([^<]+?)\s*(?:<rp>[^<]*</rp>)?\s*<rt\b[^>]*>\s*([^<]+?)\s*</rt>\s*(?:<rp>[^<]*</rp>)?\s*</ruby>)'
    html_ruby_re = re.compile(html_ruby_block + '+', re.IGNORECASE | re.DOTALL)
    
    def extract_and_replace_html(m):
        block = m.group(0)
        pairs = re.findall(html_ruby_block, block, re.IGNORECASE | re.DOTALL)
        if not pairs: return block
        
        result = []
        i = 0
        while i < len(pairs):
            matched_len = 0
            # 贪心匹配：从最长的组合开始尝试匹配词典
            for j in range(len(pairs), i, -1):
                sub_str = ''.join(p[0] for p in pairs[i:j]).strip()
                sub_str_nospace = re.sub(r'[ \u3000]', '', sub_str)
                with _dict_lock:
                    if sub_str in _exact_dict_lookup or sub_str_nospace in _exact_dict_lookup:
                        matched_len = j - i
                        break
            if matched_len > 0:
                # 命中词典，直接返回纯文本，丢弃原注音，让后面的 apply_replace_rules 接管
                result.append(''.join(p[0] for p in pairs[i:i+matched_len]))
                i += matched_len
            else:
                # 没命中，原样保留该 ruby 标签
                b, r = pairs[i]
                result.append(f"<ruby>{b}<rt>{r}</rt></ruby>")
                i += 1
        return ''.join(result)

    text = html_ruby_re.sub(extract_and_replace_html, text)
    
    # 2. 针对青空文库格式的连续 ruby (如 ｜結《ゆう》｜城《き》)
    aozora_block = r'(?:[｜|]([^《》｜|]+)《([^《》]+)》)'
    aozora_re = re.compile(aozora_block + '+')
    
    def extract_and_replace_aozora(m):
        block = m.group(0)
        pairs = re.findall(aozora_block, block)
        if not pairs: return block
        
        result = []
        i = 0
        while i < len(pairs):
            matched_len = 0
            for j in range(len(pairs), i, -1):
                sub_str = ''.join(p[0] for p in pairs[i:j]).strip()
                sub_str_nospace = re.sub(r'[ \u3000]', '', sub_str)
                with _dict_lock:
                    if sub_str in _exact_dict_lookup or sub_str_nospace in _exact_dict_lookup:
                        matched_len = j - i
                        break
            if matched_len > 0:
                result.append(''.join(p[0] for p in pairs[i:i+matched_len]))
                i += matched_len
            else:
                b, r = pairs[i]
                result.append(f"｜{b}《{r}》")
                i += 1
        return ''.join(result)
        
    text = aozora_re.sub(extract_and_replace_aozora, text)
    return text

def parse_ruby(text, output_mode='B'):
    findings = []
    
    # ★ 修复引擎误认书名号：如果《》前面不是竖线，也不是汉字，说明这只是普通书名号或强调。
    # 直接去掉《》符号只留文字，防止 TTS 引擎把前面的假名当作注音原词吞掉。
    text = re.sub(r"(?<![\|｜一-龥々〆ヵヶ])《([^《》]+)》", r"\1", text)

    # 预处理：合并相邻的 {一|いっ}{瞬|しゆん} -> {一瞬|いっしゆん}
    if '{' in text or '｛' in text:
        prev_text = ""
        while prev_text != text:
            prev_text = text
            text = re.sub(
                r'[{｛][^{}｛｝|｜]+[|｜][^{}｛｝|｜]+[}｝](?:[{｛][^{}｛｝|｜]+[|｜][^{}｛｝|｜]+[}｝])+',
                _merge_curly_braces,
                text
            )

    # 尝试合并连续的单字 ruby，如果命中词典则废弃原注音
    text = _merge_adjacent_rubies(text)

    for pattern, extractor in RUBY_PATTERNS:
        def replace_fn(m, _ext=extractor):
            base, ruby = _ext(m)
            if not base or not ruby: return m.group(0)
            
            # 修复：如果原文与注音完全一致，直接返回原词
            if base == ruby:
                return base
            
            # 核心拦截：如果发现用户词典里有这个汉字的规则，废弃原文注音
            with _dict_lock:
                base_no_space = re.sub(r'[ \u3000]', '', base)
                if base in _exact_dict_lookup or base_no_space in _exact_dict_lookup:
                    findings.append({"base": base, "ruby": ruby, "final": "由用户词典覆盖", "from_dict": True})
                    return base
                    
            def clean(r): return re.sub(r'[·・\s]', '', r)
            
            def format_c(base_str, ruby_str):
                if m.group(0).startswith('｜') or m.group(0).startswith('|'):
                    return f"｜{base_str}《{clean(ruby_str)}》"
                elif m.start() > 0 and m.string[m.start()-1] in '｜|':
                    return f"{base_str}《{clean(ruby_str)}》"
                else:
                    return f"｜{base_str}《{clean(ruby_str)}》"

            if '|' in ruby or '｜' in ruby:
                options = _parse_replacement_options(ruby)
                if len(options) > 1:
                    chosen = random.choices([o[0] for o in options], weights=[o[1] for o in options], k=1)[0]
                    findings.append({"base": base, "ruby": ruby, "final": chosen, "from_dict": False})
                    if output_mode == 'A': return base
                    elif output_mode == 'B': return chosen
                    elif output_mode == 'C': return format_c(base, chosen)
            
            findings.append({"base": base, "ruby": ruby, "final": ruby, "from_dict": False})
            if output_mode == 'A': return base
            elif output_mode == 'B': return ruby
            elif output_mode == 'C': return format_c(base, ruby)
            
        text = pattern.sub(replace_fn, text)
        
    return text, findings

def _append_to_auto_dict(base, ruby):
    global _rules_dir_state
    base, ruby = (base or "").strip(), (ruby or "").strip()
    if not base or not ruby: return False
    
    if len(base) == 1:
        return False
        
    key_check = _build_latin_fold_pattern(base) if _is_plain_text(base) else re.escape(base)
    with _dict_lock:
        if key_check in _auto_dict_keys: return False
        with open(AUTO_DICT_FILE, "a", encoding="utf-8") as f: f.write(f"{base} => {ruby}\n")
        _auto_dict_keys.add(key_check); _rules_dir_state = None
        
    with _auto_dict_lock:
        _auto_dict_words[base] = ruby
    return True

def _record_monitor(record):
    with _monitor_lock: _monitor_records.append(record)

# ============================================================
# 七、SAPI 语音 & VOICEVOX (与原版一致)
# ============================================================
def _enumerate_voices_sync():
    pythoncom.CoInitialize()
    try:
        tokens = win32com.client.Dispatch("SAPI.SpVoice").GetVoices()
        return [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
    finally: pythoncom.CoUninitialize()

def _init_voices():
    global _system_voices
    with _voices_lock:
        if not _system_voices:
            try: _system_voices = _enumerate_voices_sync()
            except Exception as e: print(f"[语音错误] {e}")
    if _system_voices: _log_sapi_models_status()

def split_text_for_sapi(text, max_len=120):
    parts = re.split(r'(?<=[。！？!?；;\n\r])', text)
    chunks, current = [], ""
    for p in parts:
        if not p: continue
        if len(current) + len(p) <= max_len: current += p
        else:
            if current: chunks.append(current)
            if len(p) > max_len:
                for sp in re.split(r'(?<=[，,、\s])', p):
                    if len(sp) > max_len:
                        while len(sp) > max_len: chunks.append(sp[:max_len]); sp = sp[max_len:]
                        chunks.append(sp)
                    else: chunks.append(sp)
                current = ""
            else: current = p
    if current: chunks.append(current)
    return [c for c in chunks if c.strip()]

def _synthesize_sapi_sync(text, voice_desc, speed, out_path):
    with _sapi_lock:
        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = speaker.GetVoices()
            target = next((tokens.Item(i) for i in range(tokens.Count) if tokens.Item(i).GetDescription() == voice_desc), None)
            if not target: raise RuntimeError(f"找不到 SAPI 语音：{voice_desc}")
            speaker.Voice = target
            speaker.Rate = max(-10, min(10, int(round((float(speed) - 1.0) * 10))))
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(out_path, 3, False)
            speaker.AudioOutputStream = stream
            for chunk in split_text_for_sapi(text): speaker.Speak(chunk)
            stream.Close()
        finally: pythoncom.CoUninitialize()

def resolve_sapi_voice(model_lower):
    if not _system_voices: raise HTTPException(status_code=503, detail="系统无可用 SAPI 语音")
    if model_lower in SPECIAL_MODELS:
        keyword = SPECIAL_MODELS[model_lower]
        for desc in _system_voices:
            if keyword.lower() in desc.lower(): return desc
        raise HTTPException(status_code=400, detail=f"未找到含「{keyword}」的语音")
    return None

async def _refresh_voicevox():
    global _voicevox_ready
    if not _http_client: return
    try:
        r = await _http_client.get(f"{VOICEVOX_URL}/version", timeout=5.0)
        r.raise_for_status()
        _voicevox_ready = True
        print(f"[VOICEVOX] 已连接")
    except Exception as e:
        _voicevox_ready = False

async def _voicevox_synth(text: str, speaker_id: int, speed: float) -> bytes:
    """
    分批向 VOICEVOX 提交文本。
    长文本单次 /audio_query 会解析非常久，甚至触发超时；
    按标点拆成多批分别合成，再把 wav 拼起来，即可大幅降低等待时间与超时风险。
    """
    if not _http_client: raise RuntimeError("HTTP 客户端未初始化")
    speed = max(0.5, min(2.0, float(speed)))

    batches = _split_text_for_batch(text, TTS_BATCH_MAX_CHARS)
    if not batches:
        raise RuntimeError("朗读文本为空")

    wav_chunks: list[bytes] = []
    for i, batch in enumerate(batches):
        try:
            r = await _http_client.post(
                f"{VOICEVOX_URL}/audio_query",
                params={"text": batch, "speaker": speaker_id},
            )
            r.raise_for_status()
            query = r.json()
            query["speedScale"] = speed
            r2 = await _http_client.post(
                f"{VOICEVOX_URL}/synthesis",
                params={"speaker": speaker_id},
                json=query,
            )
            r2.raise_for_status()
            wav_chunks.append(r2.content)
        except Exception as e:
            raise RuntimeError(
                f"VOICEVOX 请求失败（第 {i+1}/{len(batches)} 批，共 {len(batches)} 批）: {e}"
            )
    return _concat_wav_bytes(wav_chunks)

# ============================================================
# 八、FastAPI 应用与接口
# ============================================================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http_client, _browser_opened
    _init_rules_dir(); _load_rules_config(); load_all_rules(force=True)
    _init_models_config()
    _load_global_config()
    _http_client = httpx.AsyncClient(timeout=VOICEVOX_HTTP_TIMEOUT)
    await asyncio.to_thread(_init_voices); await _refresh_voicevox()
    print("服务已就绪: http://127.0.0.1:8000")
    
    # ★ 延迟 1.5 秒，等待前端网页可能的“自动重连”请求
    await asyncio.sleep(1.5)
    
    # 如果 1.5 秒内没有任何前端请求过来（当前时间距离上次请求超过 1 秒），说明网页没开着
    if not _browser_opened and time.time() - _last_frontend_ping > 1.0:
        _browser_opened = True
        html_path = pathlib.Path(BASE_DIR) / "all in one toolbox.html"
        if html_path.exists():
            print("[前端] 未检测到已打开的网页，正在唤起浏览器...")
            webbrowser.open(html_path.resolve().as_uri())
    else:
        print("[前端] 检测到网页已在后台运行，跳过自动打开。")

    try: yield
    finally:
        if _http_client: await _http_client.aclose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ★ 拦截所有前端请求，记录心跳时间
@app.middleware("http")
async def record_frontend_ping(request: Request, call_next):
    global _last_frontend_ping
    _last_frontend_ping = time.time()
    return await call_next(request)

class TTSRequest(BaseModel):
    # 默认 auto：走监控台配置，避免阅读器不传 model 时命中"未知模型"
    input: str; model: str = "auto"; voice: str = ""; speed: float = 1.0
class AutoConfigModel(BaseModel):
    model: str = "karin"; voice: str = "alloy"; speed: float = 1.0
class GlobalConfigModel(BaseModel):
    output_format: str = "C"
    lang: str = "ja"
class RulesFilesUpdate(BaseModel):
    enabled_files: list[str]
# ★ 搜索规则的请求体
class RuleSearchPayload(BaseModel):
    keyword: str
# ★ 打开文件的请求体
class RuleOpenPayload(BaseModel):
    filename: str

class ComPresetSaveModel(BaseModel):
    model_id: str; cast: str; emotions: dict[str, int]; speed: int = 50; tone_scale: int = 50
    text: str = "只今、CeVIO AI の感情パラメータのお試しテスト運転しています。"
    filename: str | None = None  # ★ 新增：允许指定保存到哪个配置文件

@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in _get_model_names()]}

@app.get("/v1/voices/windows")
async def list_windows_voices():
    """返回 Windows 系统自带的语音列表（讲述人）"""
    try:
        # 确保系统语音列表已加载
        if not _system_voices:
            await asyncio.to_thread(_init_voices)
        return {"voices": _system_voices}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/voices/edge")
async def list_edge_voices():
    """返回 Edge TTS 支持的日语和中文语音列表"""
    try:
        voices = await edge_tts.list_voices()
        # 过滤出日语(ja-)和中文(zh-)的语音
        filtered = [
            {"ShortName": v["ShortName"], "Gender": v["Gender"], "Locale": v["Locale"]}
            for v in voices if v["Locale"].startswith("ja-") or v["Locale"].startswith("zh-")
        ]
        # 按语言排序
        filtered.sort(key=lambda x: x["Locale"] + x["ShortName"])
        return {"voices": filtered}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取 Edge TTS 列表失败，请检查网络连接: {e}")

@app.post("/v1/com/speech")
async def direct_com_speech(req: ComPresetSaveModel):
    """专供前端试听的 COM 接口，不写入配置"""
    tmp_wav = os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex}.wav")
    try:
        await asyncio.to_thread(_synthesize_com_sync, req.text, req.cast, req.emotions, req.speed, req.tone_scale, tmp_wav)
        with open(tmp_wav, "rb") as f: audio = f.read()
        return Response(content=audio, media_type="audio/wav")
    finally:
        if os.path.exists(tmp_wav): os.remove(tmp_wav)

@app.get("/v1/com/casts")
async def list_com_casts():
    try: return {"casts": await asyncio.to_thread(_get_casts_sync)}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/com/emotions/{cast_name}")
async def list_com_emotions(cast_name: str):
    try: return {"cast": cast_name, "emotions": await asyncio.to_thread(_get_emotions_sync, cast_name)}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/com/preset")
async def save_com_preset(payload: ComPresetSaveModel):
    """将前端调试好的情绪参数保存为可用模型，并热重载"""
    global _active_models_filename
    
    with _models_config_lock:
        # 1. 更新内存
        CEVIO_COM_PRESETS[payload.model_id] = {
            "cast": payload.cast, "emotions": payload.emotions,
            "speed": payload.speed, "tone_scale": payload.tone_scale
        }
        
        # 2. 解析目标文件名
        name = payload.filename.strip() if payload.filename else ""
        
        if not name:
            # 留空则固定存入 my_config.json，始终保证 default 干净
            target_filename = "my_config.json"
        else:
            # 防止目录穿越
            if "/" in name or "\\" in name:
                raise HTTPException(status_code=400, detail="文件名不能包含路径符号")
            # 去除用户可能手打的 .json，防止变成 xx.json.json
            if name.lower().endswith(".json"):
                name = name[:-5]
            target_filename = name + ".json"
            
        _save_active_filename(target_filename)  # 自动切换为激活状态
        target_path = os.path.join(MODELS_CONFIG_DIR, target_filename)
        
        try:
            # 如果是全新文件，构造初始结构
            if not os.path.exists(target_path):
                data = {"voicevox": dict(MODEL_SPEAKERS), "sapi": dict(SPECIAL_MODELS), "cevio_com": {}}
            else:
                with open(target_path, "r", encoding="utf-8-sig") as f: 
                    data = json.load(f)
            
            # 写入最新的 COM 预设
            data["cevio_com"] = CEVIO_COM_PRESETS
            with open(target_path, "w", encoding="utf-8") as f: 
                json.dump(data, f, ensure_ascii=False, indent=2)
                
        except Exception as e: 
            raise HTTPException(status_code=500, detail=f"保存失败：{e}")
            
    return {"ok": True, "model_id": payload.model_id, "saved_to": target_filename}

@app.get("/v1/models/config")
async def get_models_config():
    return {"active_file": _active_models_filename, "voicevox": dict(MODEL_SPEAKERS), "sapi": dict(SPECIAL_MODELS), "cevio_com": dict(CEVIO_COM_PRESETS)}

@app.get("/v1/models/config/files")
async def list_models_config_files():
    files = [{"name": fn, "size": os.stat(os.path.join(MODELS_CONFIG_DIR, fn)).st_size, "active": fn == _active_models_filename}
             for fn in sorted(os.listdir(MODELS_CONFIG_DIR)) if fn.endswith(".json")]
    return {"dir": MODELS_CONFIG_DIR, "files": files}

@app.get("/v1/global/config")
async def get_global_config():
    return _global_config

@app.get("/v1/auto_dict")
async def get_auto_dict():
    await asyncio.to_thread(load_all_rules)
    with _auto_dict_lock:
        return {"count": len(_auto_dict_words), "words": _auto_dict_words}

@app.post("/v1/global/config")
async def set_global_config(cfg: GlobalConfigModel):
    if cfg.output_format:
        _global_config["output_format"] = cfg.output_format
    if cfg.lang:
        _global_config["lang"] = cfg.lang
    _save_global_config()
    return _global_config

@app.get("/v1/auto/config")
async def get_auto_config():
    with _auto_config_lock: return dict(_auto_config)

@app.post("/v1/auto/config")
async def set_auto_config(cfg: AutoConfigModel):
    with _auto_config_lock:
        _auto_config.update({"model": cfg.model.strip(), "voice": cfg.voice.strip(), "speed": max(0.5, min(2.0, float(cfg.speed)))})
        return dict(_auto_config)

@app.get("/v1/rules/files")
async def list_rules_files():
    txt_files = sorted(glob.glob(os.path.join(RULES_DIR, "*.txt")))
    return {"files": [{"name": os.path.basename(f), "enabled": _is_file_enabled(os.path.basename(f)), "locked": os.path.basename(f) == AUTO_DICT_BASENAME, "size": os.stat(f).st_size} for f in txt_files]}

@app.post("/v1/rules/files")
async def update_rules_files(payload: RulesFilesUpdate):
    _save_rules_config(set(payload.enabled_files))
    await asyncio.to_thread(load_all_rules, True)
    return {"ok": True}

# ★ 搜索词条存在于哪些文件
@app.post("/v1/rules/search")
async def search_rule(payload: RuleSearchPayload):
    keyword = payload.keyword.strip()
    if not keyword:
        return {"matches": []}
    
    matches = []
    if not os.path.exists(RULES_DIR):
        return {"matches": []}
        
    for filename in os.listdir(RULES_DIR):
        if filename.endswith(".txt"):
            # ★ 核心修改：不再跳过未启用的文件，全部纳入搜索范围
            is_enabled = _is_file_enabled(filename)
            file_path = os.path.join(RULES_DIR, filename)
            try:
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    for line_num, line in enumerate(f, 1):
                        line_str = line.strip()
                        # 忽略空行和注释行
                        if not line_str or line_str.startswith("#"):
                            continue
                        # 以 => 分割，检查左侧是否包含关键词
                        if "=>" in line_str:
                            left_part = line_str.split("=>")[0].strip()
                            if keyword in left_part:
                                matches.append({
                                    "file": filename,
                                    "line": f"第 {line_num} 行: {line_str}",
                                    "enabled": is_enabled  # ★ 返回该文件是否启用
                                })
            except Exception:
                pass
                
    return {"matches": matches}

# ★ 调用系统默认程序打开规则文件
@app.post("/v1/rules/open")
async def open_rule_file(payload: RuleOpenPayload):
    filename = payload.filename.strip()
    # 防止目录穿越攻击
    if not filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
        
    file_path = os.path.join(RULES_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
        
    try:
        # 仅适用于 Windows，调用系统默认程序打开（如记事本、VSCode）
        os.startfile(file_path)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败: {str(e)}")



# ============================================================
# 九、核心合成逻辑
# ============================================================
async def _do_synthesize(request: TTSRequest):
    model_raw = request.model.strip()          # ★ 保留原始大小写（Edge / SAPI 需要）
    model_lower = model_raw.lower()            # 分支判断仍用小写，不影响匹配
    voice_lower = request.voice.strip().lower()
    speed = float(request.speed)
    auto_used = False

    if model_lower == "auto":
        with _auto_config_lock: cfg = dict(_auto_config)
        model_raw = str(cfg.get("model", "auto")).strip()   # ★ 同步保留原始大小写
        model_lower = model_raw.lower()
        voice_lower = str(cfg.get("voice", "alloy")).lower()
        speed = float(cfg.get("speed", 1.0))
        auto_used = True



    # 兼容 项目旧版本残留的屎山代码
    m_openai = re.fullmatch(r'tts-(\d+)', model_lower)
    if m_openai:
        model_lower = m_openai.group(1)

    record = {
        "id": uuid.uuid4().hex, "timestamp": datetime.now().strftime("%m-%d %H:%M:%S"),
        "model": model_lower, "voice": voice_lower, "speed": speed,
        "raw_text": request.input, "auto_used": auto_used, "status": "ok", "error": ""
    }

    await asyncio.to_thread(load_all_rules)
    output_mode = _global_config.get("output_format", "C")
    lang = _global_config.get("lang", "ja")

    if lang == 'en':
        # 【英语】禁用一切映射功能，直接原文发送
        text = request.input
        record["ruby_findings"] = []
    elif lang == 'zh':
        # 【漢语】禁用仅针对日语的优化（如Ruby解析、后缀修复），只保留纯文本映射
        text = request.input
        record["ruby_findings"] = []
        # 强制使用 'B' 模式（纯文本替换），且不清理中文标点
        text = await asyncio.to_thread(apply_replace_rules, text, 'B', clean_punct=False)
    else:
        # 【日语】正常逻辑
        text, findings = await asyncio.to_thread(parse_ruby, request.input, output_mode)
        record["ruby_findings"] = findings
        for f in findings: _append_to_auto_dict(f["base"], f["ruby"])
        text = await asyncio.to_thread(fix_person_suffixes, text, output_mode)
        text = await asyncio.to_thread(apply_replace_rules, text, output_mode)
    record["final_text"] = text
    if not text.strip():
        record.update({"status": "error", "error": "朗读文本为空"}); _record_monitor(record)
        raise HTTPException(status_code=400, detail="朗读文本为空")

    # ================== ★ Windows 讲述人 / 系统 SAPI ==================
    if model_lower.startswith("windows:"):
        # 例如: "windows:Microsoft Haruka Desktop - Japanese"
        # ★ 修复：从 model_raw 提取，保留原始大小写（SAPI GetDescription 是精确匹配）
        voice_desc = model_raw[len("windows:"):].strip()
        record["backend"] = "windows_narrator"
        record["speaker"] = voice_desc
        tmp_wav = os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex}.wav")
        try:
            await asyncio.to_thread(_synthesize_sapi_sync, text, voice_desc, speed, tmp_wav)
            with open(tmp_wav, "rb") as f: audio = f.read()
            _record_monitor(record); return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"Windows合成失败: {e}"}); _record_monitor(record)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if os.path.exists(tmp_wav): os.remove(tmp_wav)

    # ================== ★ Edge TTS ==================
    elif model_lower.startswith("edge:"):
        # 例如: "edge:ja-JP-NanamiNeural"
        # ★ 修复：从 model_raw 提取，保留原始大小写（Edge TTS 的 voice_id 严格区分大小写）
        voice_id = model_raw[len("edge:"):].strip()
        record["backend"] = "edge_tts"
        record["speaker"] = voice_id
        tmp_wav = os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex}.wav")
        try:
            # 语速转换（edge-tts 支持 +0% / -50% 格式）
            rate_str = f"{int((speed - 1.0) * 100):+d}%"
            communicate = edge_tts.Communicate(text, voice_id, rate=rate_str)
            await communicate.save(tmp_wav)
            with open(tmp_wav, "rb") as f: audio = f.read()
            _record_monitor(record); return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"Edge TTS 合成失败: {e}"}); _record_monitor(record)
            raise HTTPException(status_code=500, detail=f"Edge TTS 合成失败（需联网）: {e}")
        finally:
            if os.path.exists(tmp_wav): os.remove(tmp_wav)

    # 1. VOICEVOX（直接使用纯数字作为 Speaker ID）
    if model_lower.isdigit():
        speaker_id = int(model_lower)
        if not _voicevox_ready:
            await _refresh_voicevox()
            if not _voicevox_ready: raise HTTPException(status_code=503, detail="VOICEVOX 引擎未就绪")
        
        record["backend"] = "voicevox"
        record["speaker"] = f"Speaker ID {speaker_id}"
        try:
            audio = await _voicevox_synth(text, speaker_id, speed)
            _record_monitor(record); return audio, record
        except Exception as e:
            record.update({"status": "error", "error": str(e)}); _record_monitor(record)
            raise HTTPException(status_code=500, detail=str(e))

    # 2. CeVIO COM 情绪模型
    elif model_lower in CEVIO_COM_PRESETS:
        preset = CEVIO_COM_PRESETS[model_lower]
        cast = preset.get("cast", ""); emotions = preset.get("emotions", {})
        com_speed = preset.get("speed", 50); tone_scale = preset.get("tone_scale", 50)
        record["backend"] = "cevio_com"; record["speaker"] = f"{cast} (COM)"
        tmp_wav = os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex}.wav")
        try:
            await asyncio.to_thread(_synthesize_com_sync, text, cast, emotions, com_speed, tone_scale, tmp_wav)
            with open(tmp_wav, "rb") as f: audio = f.read()
            _record_monitor(record); return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"COM 合成失败: {e}"}); _record_monitor(record)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if os.path.exists(tmp_wav): os.remove(tmp_wav)

    # 3. SAPI 原生模型
    elif model_lower in SPECIAL_MODELS:
        try: voice_desc = resolve_sapi_voice(model_lower)
        except HTTPException as e:
            record.update({"status": "error", "error": str(e.detail)}); _record_monitor(record); raise
        record["backend"] = "sapi"; record["speaker"] = voice_desc
        tmp_wav = os.path.join(tempfile.gettempdir(), f"tts_{uuid.uuid4().hex}.wav")
        try:
            await asyncio.to_thread(_synthesize_sapi_sync, text, voice_desc, speed, tmp_wav)
            with open(tmp_wav, "rb") as f: audio = f.read()
            _record_monitor(record); return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"SAPI 合成失败: {e}"}); _record_monitor(record)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if os.path.exists(tmp_wav): os.remove(tmp_wav)

    else:
        record.update({"status": "error", "error": f"未知模型: {request.model}"}); _record_monitor(record)
        raise HTTPException(status_code=400, detail=f"未知模型: {request.model}")

@app.post("/v1/audio/speech")
async def create_tts(request: TTSRequest):
    audio, _ = await _do_synthesize(request)
    return Response(content=audio, media_type="audio/wav")

@app.post("/v1/monitor/preview")
async def monitor_preview(request: TTSRequest):
    await asyncio.to_thread(load_all_rules)
    output_mode = _global_config.get("output_format", "C")
    lang = _global_config.get("lang", "ja")

    if lang == 'en':
        text = request.input
        findings = []
    elif lang == 'zh':
        text = request.input
        findings = []
        text = await asyncio.to_thread(apply_replace_rules, text, 'B', clean_punct=False)
    else:
        text, findings = await asyncio.to_thread(parse_ruby, request.input, output_mode)
        text = await asyncio.to_thread(fix_person_suffixes, text, output_mode)
        text = await asyncio.to_thread(apply_replace_rules, text, output_mode)

    return {"raw_text": request.input, "ruby_text": text, "final_text": text, "ruby_findings": findings}

@app.post("/v1/monitor/synthesize")
async def monitor_synthesize(request: TTSRequest):
    audio, record = await _do_synthesize(request)
    return {**record, "audio": base64.b64encode(audio).decode("ascii")}

@app.get("/v1/monitor/events")
async def monitor_events():
    async def event_gen():
        with _monitor_lock: snapshot = list(_monitor_records)
        yield f"data: {json.dumps({'type': 'snapshot', 'records': snapshot}, ensure_ascii=False)}\n\n"
        sent_ids = set(r["id"] for r in snapshot)
        while True:
            # ★ 核心修复：如果触发了重启或服务准备退出，立即跳出死循环，释放连接
            if _restart_requested or (_server_instance and getattr(_server_instance, "should_exit", False)):
                break
                
            await asyncio.sleep(0.4)
            with _monitor_lock: new_records = [r for r in _monitor_records if r["id"] not in sent_ids]
            for r in new_records:
                sent_ids.add(r["id"])
                yield f"data: {json.dumps({'type': 'append', 'record': r}, ensure_ascii=False)}\n\n"
            if len(sent_ids) > 200:
                with _monitor_lock: sent_ids = set(x["id"] for x in _monitor_records)
    return StreamingResponse(event_gen(), media_type="text/event-stream")

# ============================================================
# 十、快捷键：Ctrl+R / F5 重启服务
# ============================================================
def _trigger_restart():
    global _restart_requested
    if _restart_requested:
        return
    if _server_instance is None:
        print("\n[提示] 服务尚未就绪，请稍候...")
        return
    _restart_requested = True
    print()
    print("[重启] 收到快捷键，正在重启 TTS 服务...")
    sys.stdout.flush()
    _server_instance.should_exit = True


def _restart_hotkey_worker():
    """后台线程：监听 Ctrl+R / F5 重启服务"""
    time.sleep(1.5)   # 等 uvicorn 启动完成
    print()
    print("[热键] Ctrl+R 或 F5 = 重启服务 | Ctrl+C = 停止服务")
    sys.stdout.flush()
    while True:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ('\x00', '\xe0'):      # 功能键前缀
                    ch2 = msvcrt.getwch()
                    if ch2 == '\x3f':           # F5
                        _trigger_restart()
                elif ch == '\x12':              # Ctrl+R (DC2)
                    _trigger_restart()
        except Exception:
            pass
        time.sleep(0.05)


if __name__ == "__main__":
    # 启动热键监听线程
    threading.Thread(target=_restart_hotkey_worker, daemon=True).start()

    # 主循环：Ctrl+C 退出，快捷键触发重启
    while True:
        _restart_requested = False
        # ★ 增加 timeout_graceful_shutdown=3，如果 3 秒内有僵尸连接不断开，强行杀掉
        config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info", timeout_graceful_shutdown=3)
        _server_instance = uvicorn.Server(config)
        try:
            _server_instance.run()
        except KeyboardInterrupt:
            break

        if _restart_requested:
            time.sleep(0.5)
            print("[重启] 服务已重启")
            print()
            continue
        break