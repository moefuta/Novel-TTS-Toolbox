# -*- coding: utf-8 -*-
"""
Novel TTS 服务 (tts.py)
面向日文小说朗读的本地 TTS 聚合服务，兼容任意支持 OpenAI TTS 接口的阅读器
集成了：VOICEVOX、系统 SAPI、CeVIO AI (COM 情绪控制)、模型配置热重载
"""
from __future__ import annotations

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
try:
    import edge_tts
    _edge_tts_available = True
except ImportError:
    edge_tts = None
    _edge_tts_available = False
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

# ★ 黑窗口详细任务日志：统一走 stdout，确保中文/日文不乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def _log(tag: str, msg: str = ""):
    """统一日志输出到黑窗口，带时间戳与标签，方便排查映射流程。"""
    try:
        ts = datetime.now().strftime("%H:%M:%S")
    except Exception:
        ts = "--:--:--"
    line = f"[{ts}][{tag}] {msg}".rstrip()
    try:
        print(line, flush=True)
    except Exception:
        pass

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
TRASH_CAN_FILE = os.path.join(RULES_DIR, "trash_can.txt")
TRASH_CAN_BASENAME = os.path.basename(TRASH_CAN_FILE)

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
_DEFAULT_VOICEVOX = {}

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

def _generate_silence_wav(duration_sec=1.0) -> bytes:
    """生成一段静音 WAV 音频，用于处理纯标点导致的引擎合成失败"""
    framerate = 44100
    nframes = int(framerate * duration_sec)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(framerate)
        w.writeframes(b'\x00' * (nframes * 2))  # 16-bit 单声道全 0
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
# 内置默认值，可由人名词典文件（person_dict*.txt）中的 [suffix] 行扩展/覆盖
_DEFAULT_PERSON_SUFFIXES = {
    "家": {"type": "ruby", "reading": "け"},
    "は": {"type": "kana", "reading": "わ"},
}
_person_suffixes = dict(_DEFAULT_PERSON_SUFFIXES)

_exact_dict_lookup = {}  # ★ 存放纯文本规则，用于优先拦截
_auto_dict_words = {}  # ★ 存放仅作登记的 auto_dict 词条
_dict_any_word_regex = None  # ★ 用于匹配词典中任意长度>1的词，解决长词包裹问题
_auto_dict_lock = threading.Lock()
_dict_lock = threading.RLock()

# ★ auto_dict 待检验词条数量阈值播报：只在“组词”数量越过 50/100 时各播报一次
_auto_dict_announced_thresholds = set()

_rules_enabled_files = None

_trash_can_keys = set()  # ★ 废弃词表（trash_can.txt）：命中即不登记进 auto_dict
_trash_can_lock = threading.Lock()

_http_client: httpx.AsyncClient | None = None
_voicevox_ready = False

_monitor_records: deque = deque(maxlen=100)
_monitor_lock = threading.Lock()

_auto_config = {"model": "karin", "voice": "alloy", "speed": 0.9}
_auto_config_lock = threading.Lock()

_models_config_lock = threading.RLock()
_active_models_filename = "default.json"

# ★ 修改：初始化时带上默认的引擎和模型信息
_global_config = {"output_format": "C", "lang": "ja", "engine": "voicevox", "model": "1", "speed": 1.0, "theme_color": None}
_global_config_lock = threading.Lock()


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
                
            # ★ 核心修复：用 global_config 的配置覆盖可能被 my_config.json 污染的 _auto_config
            with _auto_config_lock:
                if "model" in _global_config and _global_config["model"]:
                    _auto_config["model"] = _global_config["model"]
                if "speed" in _global_config and _global_config["speed"] is not None:
                    _auto_config["speed"] = _global_config["speed"]
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
        print(f"[模型配置] 已加载主配置: {active_name}")
    except Exception as e:
        print(f"[模型配置] 加载主配置失败 {e}，使用默认")
        _apply_models_config(dict(_DEFAULT_VOICEVOX), dict(_DEFAULT_SAPI, {}))

    # ★ 核心修复：精准读取 model_configs 下的 my_config.json，强行把里面的预设拉进内存
    my_config_path = os.path.join(MODELS_CONFIG_DIR, "my_config.json")
    if os.path.exists(my_config_path):
        try:
            vv_my, sp_my, com_my = _read_models_json(my_config_path)
            if com_my:
                with _models_config_lock:
                    CEVIO_COM_PRESETS.update(com_my)
                first_model = list(com_my.keys())[0]
                with _auto_config_lock:
                    _auto_config["model"] = first_model
                print(f"[auto] 成功载入 my_config.json，已合并 {len(com_my)} 个 COM 预设，默认模型: {first_model}")
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

def _estimate_regex_match_len(pattern: str) -> int:
    """
    估算一个正则「实际消耗文本」的最大长度，用于规则引擎的「长词优先」排序。
    规则：
      - 字面量字符、字符类[...]、转义\\X、点号 . 各算 1 个字符；
      - 零宽断言 (?=...) (?!...) (?<=...) (?<!...) 算 0（不参与长词判定）；
      - 选择 a|b 取各分支长度的最大值（而不是把分支长度相加）；
      - 量词 *+?{m,n} 只作用在前一个原子上（{m,n} 取 n，其余取保守值）。
    这样 (?<![0-9])(1|一)本 会被正确估算为 2，而不是 3。
    """
    n = len(pattern)
    i = 0

    def skip_group():
        nonlocal i
        depth = 0
        while i < n:
            ch = pattern[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '[':
                i += 1
                if i < n and pattern[i] == '^':
                    i += 1
                if i < n and pattern[i] == ']':
                    i += 1
                while i < n and pattern[i] != ']':
                    i += 1
                i += 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    i += 1
                    return
            i += 1

    def parse_repeat():
        nonlocal i
        if i >= n:
            return 1
        c = pattern[i]
        if c == '*':
            i += 1
            return 1
        if c == '+':
            i += 1
            return 1
        if c == '?':
            i += 1
            return 1
        if c == '{':
            j = pattern.find('}', i)
            if j != -1:
                body = pattern[i + 1:j]
                parts = body.split(',')
                v = 0
                try:
                    if len(parts) == 2 and parts[1]:
                        v = int(parts[1])
                    elif parts[0]:
                        v = int(parts[0])
                except ValueError:
                    v = 0
                i = j + 1
                return v
        return 1

    def parse_atom():
        nonlocal i
        if i >= n:
            return 0
        c = pattern[i]
        if c == '(':
            # 零宽断言：跳过整个括号内容，算 0
            if i + 2 < n and pattern[i + 1] == '?':
                if pattern[i + 2] in '=!':
                    skip_group()
                    return 0
                if pattern[i + 2] == '<' and i + 3 < n and pattern[i + 3] in '=!':
                    skip_group()
                    return 0
            # 非捕获组 (?:...) / 命名组 (?P<name>...) / 普通组 (...)：跳过前缀，递归解析内容
            if i + 2 < n and pattern[i + 1] == '?':
                if pattern[i + 2] == ':':
                    i += 3
                elif pattern[i + 2] == 'P':
                    i += 2
                    gt = pattern.find('>', i)
                    rp = pattern.find(')', i)
                    if gt != -1 and (rp == -1 or gt < rp):
                        i = gt + 1
                else:
                    i += 1
            else:
                i += 1
            base = parse_alt()
            if i < n and pattern[i] == ')':
                i += 1
            return base
        if c == '[':
            j = i + 1
            if j < n and pattern[j] == '^':
                j += 1
            if j < n and pattern[j] == ']':
                j += 1
            while j < n and pattern[j] != ']':
                j += 1
            i = (j + 1) if j < n else n
            return 1
        if c == '\\':
            i += 2
            return 1
        if c == '.':
            i += 1
            return 1
        if c in '^$':
            i += 1
            return 0
        if c in '|)':
            return 0
        if c in '*+?{':
            i += 1
            return 0
        i += 1
        return 1

    def parse_seq():
        nonlocal i
        total = 0
        while i < n:
            c = pattern[i]
            if c == '|' or c == ')':
                break
            base = parse_atom()
            rep = parse_repeat()
            total += base * max(0, rep)
        return total

    def parse_alt():
        nonlocal i
        branches = [parse_seq()]
        while i < n and pattern[i] == '|':
            i += 1
            branches.append(parse_seq())
        return max(branches)

    try:
        result = parse_alt()
    except Exception:
        result = 0
    return max(1, result)

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
            f.write("# 仅作为二次确认的素材，防止机器错标污染全局词库。\n")
            f.write("# 请将本文件（auto_dict.txt）连同（Skill.md）一同发给DeepSeek或其他免费AI进行校验。\n")
            f.write("# 确认读音无误后，请手动将其复制到 user_dict.txt 或其他规则文件中。\n\n")
            f.write("# ===== 组词 =====\n\n")
            f.write("# ===== 单字（含やゆよつ） =====\n\n")
            f.write("# ===== 单字（不含やゆよつ） =====\n\n")
            
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

    # 5. 检查并创建 trash_can.txt（废弃词表）
    if not os.path.exists(TRASH_CAN_FILE):
        with open(TRASH_CAN_FILE, "w", encoding="utf-8") as f:
            f.write("# ==========================================\n")
            f.write("# 废弃词表（trash_can.txt）\n")
            f.write("# ==========================================\n")
            f.write("# 记录在这里的「词」，即便原文里带了它的 ruby 注音，\n")
            f.write("# 也不会再登记进 auto_dict.txt（仅此而已）。\n")
            f.write("# ⚠️ 注意：它【不会】影响朗读本身——原文自带的 ruby 注音仍会被正常读取并使用，\n")
            f.write("#   因此特别适合「多音字/多音词」：不同地方用不同注音时，别写死规则，登记到这里即可。\n#\n")
            f.write("# 用法：每行写一个词；也兼容「原文 => 读音」格式（只取左侧原文）。\n")
            f.write("# 以 # 开头的行是注释，保存后下次朗读自动热重载生效。\n\n")
            f.write("# 示例（删除行首 # 即可生效）：\n")
            f.write("# 桐ヶ谷\n")
            f.write("# 結城明日奈 => ゆうきあすな\n")

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
    if filename == TRASH_CAN_BASENAME: return True
    return True if _rules_enabled_files is None else filename in _rules_enabled_files

def _is_person_dict_file(filename: str) -> bool:
    """判断是否为「人名词典」文件：person_dict.txt 或 person_dict_xxx.txt 等带后缀的变体。"""
    name = os.path.basename(filename)
    return name == "person_dict.txt" or name.startswith("person_dict_")

def _trash_can_key(word: str) -> str:
    """生成废弃词的规范化键（与规则键一致，兼容大小写/全半角/空格）。"""
    return _build_latin_fold_pattern(word) if _is_plain_text(word) else re.escape(word)

def _load_trash_can():
    """读取 trash_can.txt，重建废弃词集合（热重载）。"""
    global _trash_can_keys
    keys = set()
    try:
        if os.path.exists(TRASH_CAN_FILE):
            with open(TRASH_CAN_FILE, "r", encoding="utf-8-sig") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    line = line.replace("＝＞", "=>").replace("=》", "=>").replace("→", "=>")
                    word = line.split("=>", 1)[0].strip() if "=>" in line else line
                    if word:
                        keys.add(_trash_can_key(word))
    except Exception:
        pass
    with _trash_can_lock:
        _trash_can_keys = keys

def _is_in_trash_can(base: str) -> bool:
    """判断一个词是否命中废弃词表（trash_can.txt）。"""
    if not _trash_can_keys:
        return False
    with _trash_can_lock:
        return _trash_can_key(base) in _trash_can_keys

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
    
    # ★ 判断是否为专用的人名词典文件（匹配 person_dict.txt 及其带后缀的变体，如 person_dict_1.txt）
    is_person_file = _is_person_dict_file(filename)

    try:
        with open(path, "r", encoding="utf-8-sig") as f: lines = f.readlines()
    except Exception as e: return None, [f"{filename} 读取失败：{e}"], set(), {}

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"): continue
        line = line.replace("＝＞", "=>").replace("=》", "=>").replace("→", "=>").replace("《=", "<=").replace("←", "<=")
        if "=>" not in line: continue
        
        # ★ 解析 [suffix] 后缀读音规则（仅人名词典文件中生效）
        if is_person_file:
            m_suffix = re.match(r'^\[suffix\]\s*(.+?)\s*=>\s*(.+)$', line)
            if m_suffix:
                orig_char = m_suffix.group(1).strip()
                reading_part = m_suffix.group(2).strip()
                reading, _ = _parse_count_suffix(reading_part)
                if orig_char and reading:
                    # ★ 核心修复：只要原词里包含假名（如“の下”），就按直接替换（kana）处理，只有纯汉字（如“君”）才用 ruby 注音。
                    is_kana_char = bool(re.search(r'[ぁ-んァ-ヶー]', orig_char))
                    suffixes[orig_char] = {
                        "type": "kana" if is_kana_char else "ruby",
                        "reading": reading,
                    }
                continue
        
        # ★ 非人名词典文件中的 [suffix] 行直接忽略，避免误解析为正则
        if line.startswith("[suffix]"):
            continue
        
        # ★ 只有在文件名符合要求时，才解析人名语法（极度宽容：一侧有括号也能识别）
        if is_person_file:
            left_side, right_side = line.split("=>", 1)
            replacement, count = _parse_count_suffix(right_side)
            
            # 尝试匹配左侧的括号 (A|B|C)
            m_person_left = re.match(r'^\((.*?)\)\s*$', left_side.strip())
            if m_person_left:
                # 右侧去括号（无论有没有括号都兼容）
                right_clean = replacement.strip()
                if right_clean.startswith('(') and right_clean.endswith(')'):
                    right_clean = right_clean[1:-1]
                
                left_parts = [p.strip() for p in m_person_left.group(1).split('|')]
                right_parts = [p.strip() for p in right_clean.split('|')]
                
                # 如果左右数量一致，或者是单对单映射
                if len(left_parts) == len(right_parts) and all(left_parts):
                    for k, v in zip(left_parts, right_parts):
                        # ★ 核心修复：强行清理首尾可能存在的竖线，防止字典录入时手滑带入
                        k = k.strip('｜|')
                        v = v.strip('｜|')
                        if not k or not v: continue

                        # ★ 核心修复：剥离括号和空格再对比，如果原文=读音，仅登记人名，绝不生成替换规则！
                        clean_k = re.sub(r'[\(\)（）\s]', '', k)
                        clean_v = re.sub(r'[\(\)（）\s]', '', v)
                        person_names.add(clean_k)
                        
                        if clean_k != clean_v and clean_v:
                            options = _parse_replacement_options(v)
                            compiled = re.compile(_build_latin_fold_pattern(k)) if _is_plain_text(k) else re.compile(k)
                            # ★ 核心修复：强制回归真实长度，严格依靠规则引擎的「长度降序」天然保证长词优先匹配！
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
            
            # ★ 核心修复：估算匹配长度（正则只按“实际消耗文本”计长，长词优先排序才正确）
            if _is_plain_text(part):
                orig_len = len(part)
            else:
                orig_len = _estimate_regex_match_len(part)
                
            rules.append((compiled, options, count, orig_len, part))
    return rules, errors, person_names, suffixes

def load_all_rules(force=False):
    global _all_rules, _rules_dir_state, _auto_dict_keys, _person_names, _exact_dict_lookup, _person_suffixes
    _load_trash_can()  # ★ 每次刷新时同步重建废弃词表（trash_can.txt 热重载）
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
        if filename == TRASH_CAN_BASENAME:
            # ★ 废弃词表（trash_can.txt）绝不参与规则解析 / ruby 解析 / TTS 判定：
            #    它只通过 _is_in_trash_can() 过滤 auto_dict 的登记，原文 ruby 注音照常读取。
            continue
        rules, _, persons, suffixes = _parse_single_file(filepath)
        if rules is None: 
            continue
            
        if is_auto:
            # ★ auto_dict.txt 只做登记，绝不参与任何 TTS 判定：
            #    只把它记录进 auto_keys / auto_dict_words，用于前端展示与 AI 校验，
            #    绝不写入 pattern_map / new_exact_dict（即 _all_rules / _exact_dict_lookup）。
            for compiled, options, count, orig_len, original_text in rules:
                auto_keys.add(compiled.pattern)
                if options:
                    new_auto_dict_words[original_text] = options[0][0]
            continue
            
        new_person_names.update(persons)
        new_person_suffixes.update(suffixes)
        
        # ★ 走到这里的一定是 person_dict / user_dict 等真实规则文件，
        #    auto_dict.txt 已在上面 continue 跳过，杜绝其参与 ruby 解析与替换判定。
        is_person = _is_person_dict_file(filename)
        for compiled, options, count, orig_len, original_text in rules:
            priority = (0, -count) if is_person else (1, -count)
                
            if compiled.pattern not in pattern_map or priority < pattern_map[compiled.pattern][0]:
                pattern_map[compiled.pattern] = (priority, compiled, options, orig_len)
                
            if _is_plain_text(original_text):
                new_exact_dict[original_text] = options
                no_space_text = re.sub(r'[ \u3000]', '', original_text)
                if no_space_text != original_text:
                    new_exact_dict[no_space_text] = options

    # --------------------------------------------------------
    # 针对早期 EPUB 解析损坏导致的连词失效问题的底层回退补丁
    # --------------------------------------------------------
    _fallback_repairs = {
        # 修复特殊排版符号引起的连读截断
        "日帝": "キチク",
        "自衛隊": "忌まわしきニッポン軍の残滓",
        "靖国神社": "せんぱんのあくしゅう",
        "靖国": "せんぱん",
    }
    
    for _k, _v in _fallback_repairs.items():
        _opts = _parse_replacement_options(_v)
        _comp = re.compile(_build_latin_fold_pattern(_k)) if _is_plain_text(_k) else re.compile(_k)
        _prio = (0, -999) 
        if _comp.pattern not in pattern_map or _prio < pattern_map[_comp.pattern][0]:
            pattern_map[_comp.pattern] = (_prio, _comp, _opts, len(_k))
        if _is_plain_text(_k):
            new_exact_dict[_k] = _opts
            _ns_k = re.sub(r'[ \u3000]', '', _k)
            if _ns_k != _k: new_exact_dict[_ns_k] = _opts

    # 合并与排序逻辑
    all_loaded_rules = [(v[1], v[2], v[3]) for v in pattern_map.values()]
    all_loaded_rules.sort(key=lambda x: x[2], reverse=True)

    with _dict_lock:
        global _dict_any_word_regex  # ★ 声明全局变量
        _all_rules = all_loaded_rules
        _auto_dict_keys = auto_keys
        _rules_dir_state = current_state
        _person_names = new_person_names
        _person_suffixes = new_person_suffixes
        _exact_dict_lookup = new_exact_dict
        
        # 生成多字词典的正则，按长度降序，保证最长匹配
        keys = [k for k in new_exact_dict.keys() if len(k) > 1]
        if keys:
            keys.sort(key=len, reverse=True)
            _dict_any_word_regex = re.compile("|".join(re.escape(k) for k in keys))
        else:
            _dict_any_word_regex = None

    with _auto_dict_lock:
        _auto_dict_words.clear()
        _auto_dict_words.update(new_auto_dict_words)

    # ★ 详细任务日志：仅在实际重建规则时输出，避免每次请求刷屏
    word_rules = [p for p in all_loaded_rules if p[2] > 1]
    _log("规则", f"已重载规则：共 {len(all_loaded_rules)} 条（多字 {len(word_rules)} 条），"
                 f"精确词表 {len(new_exact_dict)} 个，人名 {len(new_person_names)} 个，"
                 f"auto_dict 登记 {len(new_auto_dict_words)} 条")

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

    matched_rules = []  # ★ 日志：记录本次替换实际命中的规则
    for pattern, options, _ in rules_snapshot:
        def replacer(m, options=options, output_mode=output_mode, pattern=pattern):
            original = m.group(0)
            if len(options) == 1:
                chosen = options[0][0]
            else:
                texts = [o[0] for o in options]
                weights = [o[1] for o in options]
                chosen = random.choices(texts, weights=weights, k=1)[0]
                
            try: chosen_expanded = m.expand(chosen)
            except Exception: chosen_expanded = chosen
            matched_rules.append((pattern.pattern, original, chosen_expanded))
                
            if output_mode == 'A': return original
            elif output_mode == 'B': return chosen_expanded
            elif output_mode == 'C': 
                if re.match(r'^[0-9０-９]+$', original):
                    return chosen_expanded
                
                clean_ruby = re.sub(r'[·・\s]', '', chosen_expanded)
                clean_ruby = re.sub(r'^[\(（](.*?)[\)）]$', r'\1', clean_ruby)
                if clean_ruby.count('(') != clean_ruby.count(')') or clean_ruby.count('（') != clean_ruby.count('）'):
                    clean_ruby = clean_ruby.replace('(', '').replace(')', '').replace('（', '').replace('）', '')
                
                return f"｜{original}《{clean_ruby}》"

        try:
            text = pattern.sub(replacer, text)
            # ★ 关键修复：每执行完一条长词替换，立刻保护它不被后续的短词吃掉
            if output_mode == 'C':
                text = protect_rubies(text)
        except Exception: 
            pass

    # ★ 日志：汇总本次替换命中的规则（去重，避免刷屏）
    if matched_rules:
        seen_rules = []
        for pat, original, chosen in matched_rules:
            if (pat, original, chosen) not in seen_rules:
                seen_rules.append((pat, original, chosen))
        for pat, original, chosen in seen_rules[:20]:
            _log("替换", f"规则命中: {original} => {chosen}  (pattern={pat})")
        if len(seen_rules) > 20:
            _log("替换", f"…… 共命中 {len(seen_rules)} 条规则，仅显示前 20 条")
            
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
        
    # ★ 核心修复：直接移除间隔号 (・)，避免 TTS 引擎将其误认为是长停顿的逗号/顿号，确保外来语复合词完美连读。
    text = text.replace("・", "").replace("·", "") 
    
    # 合并连续标点并去除首尾标点
    text = re.sub(r"([、，,])[、，,]+", r"\1", text)
    return re.sub(r"^[、，,]+|[、，,]+$", "", text)


def fix_person_suffixes(text, output_mode='C'):
    """
    自动修正人名后面的称呼/助词后缀读音。
    后缀映射表由人名词典文件（person_dict*.txt）中的 [suffix] 行定义（内置默认：家→け、は→わ）。
    """
    with _dict_lock:
        if not _person_names: return text
        names_snapshot = list(_person_names)
        suffix_map = dict(_person_suffixes)
        exact_dict = dict(_exact_dict_lookup)
        
    if not names_snapshot or not suffix_map: return text
    
    # 名字按长度降序，优先匹配长名字
    sorted_names = sorted(names_snapshot, key=len, reverse=True)
    name_group = '(' + '|'.join(re.escape(n) for n in sorted_names) + ')'
    
    # 后缀按长度降序，优先匹配长后缀
    sorted_suffixes = sorted(suffix_map.keys(), key=len, reverse=True)
    suffix_group = '(' + '|'.join(re.escape(s) for s in sorted_suffixes) + ')'
    
    pattern = re.compile(
        name_group 
        + r'(?:《[^》]*》)?'  
        + r'[\s｜|]*'       
        + suffix_group 
        + r'(?:《[^》]*》)?'  
    )
    
    def replacer(m):
        full = m.group(0)
        
        # ★ 终极防御 1：如果匹配到的字符串尾部带有注音框，且注音的后缀读音与当前后缀一致
        # 这完美拦截了 '父様《おとうさま》'，坚决不拆
        m_ruby_end = re.search(r'《([^》]*)》$', full)
        if m_ruby_end:
            ruby_content = m_ruby_end.group(1)
            info = suffix_map.get(m.group(2))
            if info and ruby_content.endswith(info["reading"]):
                return full

        # ★ 终极防御 2：如果匹配到的完整字符串本身就是一个已登记的人名或完整词
        clean_full = re.sub(r'^[｜|]', '', full)
        clean_full = re.sub(r'《[^》]*》', '', clean_full)
        if clean_full in names_snapshot or clean_full in exact_dict:
            return full

        name_str = m.group(1)
        suffix_str = m.group(2)
        
        info = suffix_map.get(suffix_str)
        if info is None: return full
        
        suffix_idx = full.rfind(suffix_str)
        if suffix_idx == -1: return full
        
        name_part = full[:suffix_idx].rstrip('｜| \t\n\r')
        
        if not name_part.startswith('｜') and not name_part.startswith('|') and '《' in name_part:
            name_part = '｜' + name_part
            
        if info["type"] == "ruby":
            if output_mode == 'C':
                res = f'｜{suffix_str}《{info["reading"]}》'
            elif output_mode == 'B':
                res = info["reading"]
            else:
                res = suffix_str
        else:  
            res = info["reading"]

        _log("后缀", f"修正人名后缀: {full} -> {name_part + res}")
        return name_part + res
        
    return pattern.sub(replacer, text)

# ============================================================
# 六、Ruby 解析 (与原版一致)
# ============================================================
RUBY_KANA = r"[ぁ-んァ-ヶーヽヾゝゞ]+"
RUBY_KANA = r"[ぁ-んァ-ヶーヽヾゝゞ]+"

def _extract_html_ruby(m):
    """
    解析 <ruby>...</ruby>，支持一个 ruby 内部有多个 <rt>：
      <ruby>矯<rt>きよう</rt>正<rt>せい</rt></ruby>  →  base="矯正",  ruby="きようせい"
      <ruby>判<rt>わか</rt></ruby>                  →  base="判",    ruby="わか"
    """
    inner = m.group(1)
    # 先剥离 <rp> 标签（内含标注，不参与读音）
    inner_clean = re.sub(r"<rp\b[^>]*>[\s\S]*?</rp>", "", inner, flags=re.IGNORECASE)
    # 以 <rt>...</rt> 为分隔符切片：偶数位是 base，奇数位是 ruby
    parts = re.split(r"<rt\b[^>]*>([\s\S]*?)</rt>", inner_clean, flags=re.IGNORECASE)
    base = "".join(parts[0::2])
    ruby = "".join(parts[1::2])
    # 去掉残留的其它标签（如 <rb>、<span> 之类），再清理空白
    base = re.sub(r"<[^>]+>", "", base).strip()
    ruby = re.sub(r"<[^>]+>", "", ruby).strip()
    return base, ruby

RUBY_PATTERNS = [
    (re.compile(r"<ruby\b[^>]*>([\s\S]*?)</ruby>", re.IGNORECASE | re.DOTALL), _extract_html_ruby),
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
                merged_base = ''.join(p[0] for p in pairs[i:i+matched_len])
                _log("Ruby", f"ruby 命中词典，丢弃原注音（交由词库替换接管）: {merged_base}")
                result.append(merged_base)
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
                merged_base = ''.join(p[0] for p in pairs[i:i+matched_len])
                _log("Ruby", f"ruby 命中词典，丢弃原注音（交由词库替换接管）: {merged_base}")
                result.append(merged_base)
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
    _seen_findings = set()  # ★ 新增：用于去重，记录 (base, ruby, final, from_dict) 的组合
    
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
            
            if base == ruby:
                _log("Ruby", f"丢弃注音（原文=读音）: {base} => {ruby}")
                return base
                
            # ★ 核心新增：自动屏蔽纯英文/数字/符号的注音
            # 如果注音是由纯英文字母、全角字母、数字、空格或连字符组成
            # 直接废弃该注音，返回原汉字，防止日文引擎逐字母拼读 (如 S-H-A-R-P)
            if re.fullmatch(r'[A-Za-zＡ-Ｚａ-ｚ0-9０-９\s\-]+', ruby):
                key = (base, ruby, "已屏蔽英文注音", True)
                if key not in _seen_findings:
                    _seen_findings.add(key)
                    _log("Ruby", f"屏蔽英文注音: {base} => {ruby}")
                    findings.append({"base": base, "ruby": ruby, "final": "已屏蔽英文注音", "from_dict": True})
                return base
                
            # 核心拦截：如果发现用户词典里有这个汉字的规则，废弃原文注音
            with _dict_lock:
                base_no_space = re.sub(r'[ \u3000]', '', base)
                if base in _exact_dict_lookup or base_no_space in _exact_dict_lookup:
                    _log("Ruby", f"由用户词典覆盖，丢弃原文注音: {base} => {ruby}")
                    key = (base, ruby, "由用户词典覆盖", True)
                    if key not in _seen_findings:
                        _seen_findings.add(key)
                        findings.append({"base": base, "ruby": ruby, "final": "由用户词典覆盖", "from_dict": True})
                    return base
                
                # ★ 核心修复：如果该词在 person_suffixes 中，说明它是待修正的后缀，直接废弃原注音
                if base in _person_suffixes:
                    _log("Ruby", f"由后缀词典覆盖，丢弃原文注音: {base} => {ruby}")
                    key = (base, ruby, "由后缀词典覆盖", True)
                    if key not in _seen_findings:
                        _seen_findings.add(key)
                        findings.append({"base": base, "ruby": ruby, "final": "由后缀词典覆盖", "from_dict": True})
                    return base
                
                # ★ 拦截：如果 base 的某个子串在词典中（如 施錠音 包含 施錠），也废弃注音，交由后续规则处理
                if _dict_any_word_regex and _dict_any_word_regex.search(base):
                    _log("Ruby", f"包含词典词，拆解由词典接管，丢弃原文注音: {base} => {ruby}")
                    key = (base, ruby, "包含词典词，已拆解由词典接管", True)
                    if key not in _seen_findings:
                        _seen_findings.add(key)
                        findings.append({"base": base, "ruby": ruby, "final": "包含词典词，已拆解由词典接管", "from_dict": True})
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
                    key = (base, ruby, chosen, False)
                    if key not in _seen_findings:
                        _seen_findings.add(key)
                        _log("Ruby", f"保留注音（多读音随机抽取）: {base} => {ruby} → 抽中 {chosen}")
                        findings.append({"base": base, "ruby": ruby, "final": chosen, "from_dict": False})
                    if output_mode == 'A': return base
                    elif output_mode == 'B': return chosen
                    elif output_mode == 'C': return format_c(base, chosen)
            
            key = (base, ruby, ruby, False)
            if key not in _seen_findings:
                _seen_findings.add(key)
                _log("Ruby", f"保留注音: {base} => {ruby}")
                findings.append({"base": base, "ruby": ruby, "final": ruby, "from_dict": False})

            if output_mode == 'A': return base
            elif output_mode == 'B': return ruby
            elif output_mode == 'C': return format_c(base, ruby)
            
        text = pattern.sub(replace_fn, text)
        
    return text, findings

_YA_YU_YO_TSU_RE = re.compile(r'[やゆよつゃゅょっヤユヨツャュョッ]')

def _contains_yayuyotsu(ruby: str) -> bool:
    """判断读音是否包含「やゆよつ」系假名（含大小字、平片假名），用于单字分组。"""
    return bool(_YA_YU_YO_TSU_RE.search(ruby))

def _append_to_auto_dict(base, ruby):
    global _rules_dir_state
    base, ruby = (base or "").strip(), (ruby or "").strip()
    if not base or not ruby: return False
    
    # ★ 废弃词表：命中 trash_can.txt 即直接跳过「登记」这一步。
    #    它只影响 auto_dict 登记，绝不回头改动已经解析好的朗读文本（ruby 注音照常用）。
    if _is_in_trash_can(base):
        _log("登记", f"跳过（命中 trash_can.txt，仅不登记，不影响朗读）: {base} => {ruby}")
        return False
    
    # ★ 收录所有单字（不再按高危条件过滤），写入时按「含/不含 やゆよつ」分组

    with _dict_lock:
        # 终极防线：如果这个词在词典里已经存在（哪怕大小写/空格有差异），直接拒绝登记
        base_no_space = re.sub(r'[ \u3000]', '', base)
        if base in _exact_dict_lookup or base_no_space in _exact_dict_lookup:
            _log("登记", f"跳过（已被现有词典覆盖）: {base} => {ruby}")
            return False

    key_check = _build_latin_fold_pattern(base) if _is_plain_text(base) else re.escape(base)
    with _dict_lock:
        # 即使 key 已经存在于内存，如果是单字，我们依然需要检查是否需要追加读音
        # if key_check in _auto_dict_keys: return False  <- 注释掉这行，让逻辑走下去

        lines = []
        # 物理检查文件，防止重复追加，并读取现有内容以便分类插入
        if os.path.exists(AUTO_DICT_FILE):
            try:
                with open(AUTO_DICT_FILE, "r", encoding="utf-8-sig") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines):
                    if "=>" in line:
                        parts = line.split("=>", 1)
                        ex_base = parts[0].strip()
                        ex_ruby = parts[1].strip()
                        ex_key = _build_latin_fold_pattern(ex_base) if _is_plain_text(ex_base) else re.escape(ex_base)
                        
                        if ex_key == key_check:
                            _auto_dict_keys.add(key_check)
                            
                            # ★ 核心新增：如果是单字，且出现了不同的读音，则进行合并更新！
                            if len(base) == 1 and ex_ruby != ruby:
                                # 拆分现有的读音（兼容带 | 的情况）
                                ex_rubies = [r.strip() for r in ex_ruby.split('|')]
                                if ruby not in ex_rubies:
                                    # 合并并更新文件
                                    new_ruby_str = f"{ex_ruby}|{ruby}"
                                    new_line = f"{ex_base} => {new_ruby_str}\n"
                                    lines[i] = new_line
                                    
                                    with open(AUTO_DICT_FILE, "w", encoding="utf-8") as f:
                                        f.writelines(lines)
                                    
                                    # 实时同步更新内存中的 auto_dict_words，让前端立刻能刷新到
                                    with _auto_dict_lock:
                                        _auto_dict_words[ex_base] = new_ruby_str
                                    
                                    _rules_dir_state = None # 触发规则重载
                                    _log("登记", f"单字合并读音: {ex_base} => {new_ruby_str}")
                                    return True
                            
                            _log("登记", f"跳过（auto_dict 已登记过）: {base} => {ruby}")
                            return False # 已处理或不需要处理，直接结束
            except Exception:
                pass
                
        # 如果文件是空的，先建个基础骨架
        if not lines:
            lines = [
                "# ==========================================\n",
                "# 自动记忆词典（auto_dict.txt）\n",
                "# ==========================================\n\n",
                "# ===== 组词 =====\n",
                "\n",
                "# ===== 单字（含やゆよつ） =====\n",
                "\n",
                "# ===== 单字（不含やゆよつ） =====\n",
                "\n"
            ]

        # 根据长度与读音决定放入哪个分类
        if len(base) > 1:
            target_title = "# ===== 组词 =====\n"
        elif _contains_yayuyotsu(ruby):
            target_title = "# ===== 单字（含やゆよつ） =====\n"
        else:
            target_title = "# ===== 单字（不含やゆよつ） =====\n"
        new_line = f"{base} => {ruby}\n"
        
        # 确保标题存在
        if not any(target_title.strip() == l.strip() for l in lines):
            lines.append(f"\n{target_title}\n")
            
        # 寻找插入位置（在目标标题下方的最后一个条目之后）
        insert_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == target_title.strip():
                insert_idx = i + 1
                # 一直往下找，直到遇到下一个标题（# 开头）
                while insert_idx < len(lines) and not lines[insert_idx].startswith("#"):
                    insert_idx += 1
                break
                
        if insert_idx == -1:
            lines.append(new_line)
        else:
            lines.insert(insert_idx, new_line)
            
        # 覆盖写入文件
        with open(AUTO_DICT_FILE, "w", encoding="utf-8") as f: 
            f.writelines(lines)
            
        _auto_dict_keys.add(key_check)
        _rules_dir_state = None
        
    with _auto_dict_lock:
        _auto_dict_words[base] = ruby
    _log("登记", f"已登记进 auto_dict: {base} => {ruby}")
    return True

# ★ 待检验词条（auto_dict 组词）数量阈值播报
_AUTO_DICT_ANNOUNCE_THRESHOLDS = (50, 100)
_AUTO_DICT_ANNOUNCE_NUMBERS = {50: "五十", 100: "百"}

def _current_auto_dict_word_count() -> int:
    """当前待检验『组词』数量（只统计长度 > 1 的词条，与前端主计数一致）。"""
    with _auto_dict_lock:
        return sum(1 for k in _auto_dict_words.keys() if len(k) > 1)

def _auto_dict_announce_if_crossed(before_count: int, after_count: int) -> int | None:
    """
    判断本次登记是否让待检验『组词』数量越过 50 / 100 阈值。
    每个阈值整个会话只播报一次，避免反复提醒打扰朗读。
    """
    for t in _AUTO_DICT_ANNOUNCE_THRESHOLDS:
        if before_count < t <= after_count and t not in _auto_dict_announced_thresholds:
            _auto_dict_announced_thresholds.add(t)
            return t
    return None

def _build_auto_dict_announcement(threshold: int, output_mode: str = 'C') -> str:
    """按当前输出形式构造阈值提醒文本（A=原文 / B=纯读音 / C=ruby 注音）。"""
    num = _AUTO_DICT_ANNOUNCE_NUMBERS.get(threshold, str(threshold))

    def seg(base, ruby):
        if base is None:
            return ruby
        if output_mode == 'C':
            return f"｜{base}《{ruby}》"
        if output_mode == 'B':
            return ruby
        return base  # output_mode == 'A'

    return (
        seg(None, "ただいま、")
        + seg("確認", "かくにん") + seg(None, "待ちの")
        + seg("項目", "こうもく") + seg(None, "わ、")
        + num + seg(None, "になりました、")
        + seg("早急", "そうきゅう") + seg(None, "に")
        + seg("確認", "かくにん") + seg(None, "する事をお勧めします。")
    )

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
    _load_global_config()  # ★ 核心修复：启动时必须将 global_config.json 读取到内存，否则前端读到的永远是默认值！
    _http_client = httpx.AsyncClient(timeout=VOICEVOX_HTTP_TIMEOUT)
    await asyncio.to_thread(_init_voices); await _refresh_voicevox()
    
    print("\n" + "="*60)
    print("🎉 TTS 服务已就绪: http://127.0.0.1:8000")
    print("👉 请手动双击打开同目录下的 'launcher.html' 以使用控制台。")
    print("="*60 + "\n")

    try: yield
    finally:
        if _http_client: await _http_client.aclose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class TTSRequest(BaseModel):
    # 默认 auto：走监控台配置，避免阅读器不传 model 时命中"未知模型"
    input: str; model: str = "auto"; voice: str = ""; speed: float = 1.0
class AutoConfigModel(BaseModel):
    model: str = "karin"; voice: str = "alloy"; speed: float = 1.0
class GlobalConfigModel(BaseModel):
    output_format: str | None = None
    lang: str | None = None
    engine: str | None = None
    model: str | None = None
    speed: float | None = None
    theme_color: str | None = None
class RulesFilesUpdate(BaseModel):
    enabled_files: list[str]

# ★ 删除 auto_dict 词条的请求体
class AutoDictDeletePayload(BaseModel):
    base: str
    ruby: str

# ★ 搜索规则的请求体
class RuleSearchPayload(BaseModel):
    keyword: str
    mode: str = "all"  # 可选值：all (全搜索), base (搜A), ruby (搜B)
# ★ 删除 auto_dict 分组请求体
class AutoDictDeleteGroupPayload(BaseModel):
    group: str  # "word" / "single_ya" / "single_no"
# ★ 打开文件的请求体
class RuleOpenPayload(BaseModel):
    filename: str

class UiOrderUpdate(BaseModel):
    order: dict[str, list[str]]   # 例如 {"monitorCevioModel": ["karin", ...], ...}

class ComPresetSaveModel(BaseModel):
    model_id: str; cast: str; emotions: dict[str, int]; speed: int = 50; tone_scale: int = 50
    text: str = "只今、CeVIO AI の感情パラメータのお試しテスト運転しています。"
    filename: str | None = None  # ★ 新增：允许指定保存到哪个配置文件

def _read_app_version() -> str:
    """读取 html_resources/update_history.html，解析最新版本号（如 v1.1.3）。"""
    history_path = os.path.join(BASE_DIR, "html_resources", "update_history.html")
    try:
        if os.path.exists(history_path):
            with open(history_path, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r'class="cl-title"[^>]*>\s*(v?\d+\.\d+\.\d+)', content)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "v1.1.3"

@app.get("/v1/version")
async def get_app_version():
    version = _read_app_version()
    return {"version": version, "name": f"Novel TTS Toolbox {version}"}

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
    if not _edge_tts_available:
        raise HTTPException(status_code=500, detail="未安装 edge-tts 库，请在黑窗口运行: py -m pip install edge-tts")
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

@app.get("/v1/models/config/order")
async def get_ui_order():
    """读取当前激活配置里保存的下拉菜单排序偏好。"""
    with _models_config_lock:
        target_path = os.path.join(MODELS_CONFIG_DIR, _active_models_filename)
        if not os.path.exists(target_path):
            return {"order": {}}
        try:
            with open(target_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return {"order": data.get("_ui_order", {})}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取排序失败: {e}")


@app.post("/v1/models/config/order")
async def save_ui_order(payload: UiOrderUpdate):
    """把前端拖拽后的顺序持久化到当前激活配置文件里，字段名 `_ui_order`。"""
    with _models_config_lock:
        target_path = os.path.join(MODELS_CONFIG_DIR, _active_models_filename)
        if not os.path.exists(target_path):
            raise HTTPException(status_code=404,
                                detail=f"激活的配置文件不存在: {_active_models_filename}")
        try:
            with open(target_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")

        # 合并而不是覆盖：不同下拉分别提交也能累积保存
        merged = dict(data.get("_ui_order", {}))
        for key, arr in payload.order.items():
            if isinstance(arr, list):
                merged[key] = [str(x) for x in arr]
        data["_ui_order"] = merged

        try:
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"保存排序失败: {e}")

        return {"ok": True, "order": merged}

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
        # 只统计组词（长度 > 1），单字不计入主计数，作为纯占位使用
        word_count = sum(1 for k in _auto_dict_words.keys() if len(k) > 1)
        single_count = sum(1 for k in _auto_dict_words.keys() if len(k) == 1)
        return {
            "count": word_count,          # 前端显示的数字
            "single_count": single_count, # 单字占位数量
            "total_count": len(_auto_dict_words),
            "words": _auto_dict_words
        }

@app.post("/v1/auto_dict/delete")
async def delete_auto_dict_item(payload: AutoDictDeletePayload):
    base = payload.base.strip()
    ruby = payload.ruby.strip()
    if not base or not ruby:
        raise HTTPException(status_code=400, detail="词条内容为空")
        
    target_line = f"{base} => {ruby}"
    
    with _auto_dict_lock:
        if not os.path.exists(AUTO_DICT_FILE):
            raise HTTPException(status_code=404, detail="文件不存在")
            
        try:
            with open(AUTO_DICT_FILE, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
                
            new_lines = []
            deleted = False
            for line in lines:
                # 精确匹配整行（去除首尾空白后对比），将其删除
                if line.strip() == target_line:
                    deleted = True
                    continue
                new_lines.append(line)
                
            if deleted:
                with open(AUTO_DICT_FILE, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                
                # ★ 核心修复：直接实时同步内存字典（外层已经加锁，不可重入）
                if base in _auto_dict_words:
                    del _auto_dict_words[base]
                        
                return {"ok": True, "msg": f"已删除: {target_line}"}
            else:
                raise HTTPException(status_code=404, detail="未找到该词条，可能已被删除")
                
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")

@app.post("/v1/auto_dict/delete_group")
async def delete_auto_dict_group(payload: AutoDictDeleteGroupPayload):
    group = payload.group
    if group not in ("word", "single_ya", "single_no"):
        raise HTTPException(status_code=400, detail="非法分组名")

    with _auto_dict_lock:
        if not os.path.exists(AUTO_DICT_FILE):
            raise HTTPException(status_code=404, detail="文件不存在")

        try:
            with open(AUTO_DICT_FILE, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()

            new_lines = []
            current_section = None
            deleted_count = 0

            for line in lines:
                stripped = line.strip()
                # 识别当前处于哪个分区
                if stripped.startswith("# ===== 组词 ====="):
                    current_section = "word"
                    new_lines.append(line)
                    continue
                elif stripped.startswith("# ===== 单字（含やゆよつ） ====="):
                    current_section = "single_ya"
                    new_lines.append(line)
                    continue
                elif stripped.startswith("# ===== 单字（不含やゆよつ） ====="):
                    current_section = "single_no"
                    new_lines.append(line)
                    continue
                elif stripped.startswith("#"):
                    current_section = None
                    new_lines.append(line)
                    continue

                # 如果当前行属于要清空的分区，且是有效规则
                if current_section == group and "=>" in stripped:
                    deleted_count += 1
                    continue # 直接跳过，相当于删除

                new_lines.append(line)

            if deleted_count > 0:
                with open(AUTO_DICT_FILE, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)

                # 同步清理内存中的字典
                keys_to_delete = []
                for k, v in _auto_dict_words.items():
                    if len(k) > 1:
                        if group == "word":
                            keys_to_delete.append(k)
                    else:
                        if group == "single_ya" and _contains_yayuyotsu(v):
                            keys_to_delete.append(k)
                        elif group == "single_no" and not _contains_yayuyotsu(v):
                            keys_to_delete.append(k)
                for k in keys_to_delete:
                    del _auto_dict_words[k]

                return {"ok": True, "msg": f"已清空 {deleted_count} 条 {group} 词条"}
            else:
                return {"ok": True, "msg": "该分组已经是空的"}

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"清空失败: {str(e)}")

@app.post("/v1/global/config")
async def set_global_config(cfg: GlobalConfigModel):
    if cfg.output_format:
        _global_config["output_format"] = cfg.output_format
    if cfg.lang:
        _global_config["lang"] = cfg.lang
    # ★ 持久化引擎、模型与速度信息
    if cfg.engine is not None:
        _global_config["engine"] = cfg.engine
    if cfg.model is not None:
        _global_config["model"] = cfg.model
    if cfg.speed is not None:
        _global_config["speed"] = cfg.speed
    if cfg.theme_color is not None:
        _global_config["theme_color"] = cfg.theme_color
        
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
    return {"files": [{"name": os.path.basename(f), "enabled": _is_file_enabled(os.path.basename(f)), "locked": os.path.basename(f) in (AUTO_DICT_BASENAME, TRASH_CAN_BASENAME), "size": os.stat(f).st_size} for f in txt_files]}

@app.post("/v1/rules/files")
async def update_rules_files(payload: RulesFilesUpdate):
    _save_rules_config(set(payload.enabled_files))
    await asyncio.to_thread(load_all_rules, True)
    return {"ok": True}

# ★ 搜索词条存在于哪些文件
@app.post("/v1/rules/search")
async def search_rule(payload: RuleSearchPayload):
    keyword = payload.keyword.strip()
    mode = payload.mode.lower()
    if not keyword:
        return {"matches": []}
    
    matches = []
    if not os.path.exists(RULES_DIR):
        return {"matches": []}
        
    for filename in os.listdir(RULES_DIR):
        if filename.endswith(".txt"):
            # 不再跳过未启用的文件，全部纳入搜索范围
            is_enabled = _is_file_enabled(filename)
            file_path = os.path.join(RULES_DIR, filename)
            try:
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    for line_num, line in enumerate(f, 1):
                        line_str = line.strip()
                        # 忽略空行和注释行
                        if not line_str or line_str.startswith("#"):
                            continue
                        
                        # 以 => 分割，区分 A（左侧）和 B（右侧）
                        if "=>" in line_str:
                            parts = line_str.split("=>", 1)
                            left_part = parts[0].strip()
                            right_part = parts[1].strip()
                            
                            match_found = False
                            if mode == "base":
                                match_found = (keyword in left_part)
                            elif mode == "ruby":
                                match_found = (keyword in right_part)
                            else:  # all
                                match_found = (keyword in left_part) or (keyword in right_part)
                                
                            if match_found:
                                matches.append({
                                    "file": filename,
                                    "line": f"第 {line_num} 行: {line_str}",
                                    "enabled": is_enabled
                                })
            except Exception:
                pass
                
    return {"matches": matches}

# ★ 调用系统默认程序打开规则文件或特定文件夹
@app.post("/v1/rules/open")
async def open_rule_file(payload: RuleOpenPayload):
    filename = payload.filename.strip()
    
    # 拦截特殊目录请求
    if filename in ("bg", "../bg"):
        file_path = BG_DIR
    elif filename in (".", "./"):
        file_path = RULES_DIR
    else:
        if not filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="非法文件名")
        file_path = os.path.join(RULES_DIR, filename)
        
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件夹不存在: {file_path}")
        
    try:
        os.startfile(file_path)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败: {str(e)}")


# ============================================================
# 九、核心合成逻辑
# ============================================================
def _is_fully_covered_by_dict(text: str) -> bool:
    """
    判断一个词是否被现有规则库完全覆盖。
    例如词典里有 (茅場|晶彦) => (かやば|あきひこ)，
    传入 "茅場晶彦" 运行替换后得到 "かやばあきひこ"，
    如果没有残留任何汉字，说明已被完全覆盖。
    """
    replaced = apply_replace_rules(text, 'B', clean_punct=False)
    if replaced == text:
        return False  # 原文没变，说明根本没命中词典
    if re.search(r'[一-龥々〆ヵヶ]', replaced):
        return False  # 替换后还有汉字，说明只覆盖了一部分（如只把“茅場”转了）
    return True  # 被彻底转为假名/罗马音，说明完全覆盖

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

    _t_start = time.time()
    _log("请求", f"开始合成: model={model_lower} lang={lang} output_format={output_mode}")
    _log("请求", f"原文: {request.input}")

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
        _log("Ruby", f"共识别 {len(findings)} 条注音")
        _log("Ruby", f"解析后文本: {text}")
        
        # ★ 修复：在登记 auto_dict 前，检查是否已被现有词典完全接管
        count_before = _current_auto_dict_word_count()
        for f in findings:
            if f.get("from_dict"):
                continue
            base = f["base"]
            # 如果这个组合词能被现有规则库完美拆解并替换（不留任何汉字），则不登记
            if await asyncio.to_thread(_is_fully_covered_by_dict, base):
                _log("登记", f"跳过（已被现有词典完全拆解覆盖）: {base} => {f['ruby']}")
                continue
            _append_to_auto_dict(base, f["ruby"])
        count_after = _current_auto_dict_word_count()

        # ★ 核心修复：先执行词库替换（长词优先拦截，防止被后缀修正拆解），再执行后缀修正
        text = await asyncio.to_thread(apply_replace_rules, text, output_mode)
        _log("替换", f"词库替换后文本: {text}")
        text = await asyncio.to_thread(fix_person_suffixes, text, output_mode)
        _log("后缀", f"后缀修正后文本: {text}")

        # ★ 新功能：待检验词条数量越过 50 / 100 时，临时在正文前插入一条语音提醒
        threshold = _auto_dict_announce_if_crossed(count_before, count_after)
        if threshold is not None:
            announcement = _build_auto_dict_announcement(threshold, output_mode)
            _log("提醒", f"待检验词条已达 {threshold} 条，临时插入语音提醒")
            text = announcement + text

    record["final_text"] = text
    _log("合成", f"最终文本: {text}")
    if not text.strip():
        record.update({"status": "error", "error": "朗读文本为空"}); _record_monitor(record)
        raise HTTPException(status_code=400, detail="朗读文本为空")

    # ★ 修复：拦截纯标点文本（如 ──…… ），直接返回 1 秒静音，防止 COM 引擎报错
    # 正则含义：如果文本中找不到任何汉字、假名或英文字母/数字，说明全是标点符号
    if not re.search(r'[0-9a-zA-Z\u3040-\u30ff\u4e00-\u9fff]', text):
        record["backend"] = "silence"
        record["speaker"] = "纯标点静音兜底"
        _record_monitor(record)
        _log("合成", "纯标点文本，返回 1 秒静音兜底")
        return _generate_silence_wav(1.0), record

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
            _record_monitor(record)
            _log("合成", f"Windows 讲述人合成成功，耗时 {int((time.time()-_t_start)*1000)}ms")
            return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"Windows合成失败: {e}"}); _record_monitor(record)
            _log("合成", f"Windows 讲述人合成失败: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if os.path.exists(tmp_wav): os.remove(tmp_wav)

    # ================== ★ Edge TTS ==================
    elif model_lower.startswith("edge:"):
        if not _edge_tts_available:
            raise HTTPException(status_code=500, detail="未安装 edge-tts 库，请运行 pip install edge-tts")
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
            _record_monitor(record)
            _log("合成", f"Edge TTS 合成成功，耗时 {int((time.time()-_t_start)*1000)}ms")
            return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"Edge TTS 合成失败: {e}"}); _record_monitor(record)
            _log("合成", f"Edge TTS 合成失败: {e}")
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
            _record_monitor(record)
            _log("合成", f"VOICEVOX 合成成功，耗时 {int((time.time()-_t_start)*1000)}ms")
            return audio, record
        except Exception as e:
            record.update({"status": "error", "error": str(e)}); _record_monitor(record)
            _log("合成", f"VOICEVOX 合成失败: {e}")
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
            _record_monitor(record)
            _log("合成", f"CeVIO COM 合成成功，耗时 {int((time.time()-_t_start)*1000)}ms")
            return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"COM 合成失败: {e}"}); _record_monitor(record)
            _log("合成", f"CeVIO COM 合成失败: {e}")
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
            _record_monitor(record)
            _log("合成", f"SAPI 合成成功，耗时 {int((time.time()-_t_start)*1000)}ms")
            return audio, record
        except Exception as e:
            record.update({"status": "error", "error": f"SAPI 合成失败: {e}"}); _record_monitor(record)
            _log("合成", f"SAPI 合成失败: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if os.path.exists(tmp_wav): os.remove(tmp_wav)

    else:
        # ★ 核心修复：报错应该显示解析后的 model_lower，而不是外层的 auto
        record.update({"status": "error", "error": f"未知模型: {model_lower}"}); _record_monitor(record)
        _log("合成", f"未知模型: {model_lower}")
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
    _log("请求", f"预览: lang={lang} output_format={output_mode} 原文: {request.input}")

    if lang == 'en':
        text = request.input
        findings = []
    elif lang == 'zh':
        text = request.input
        findings = []
        text = await asyncio.to_thread(apply_replace_rules, text, 'B', clean_punct=False)
    else:
        text, findings = await asyncio.to_thread(parse_ruby, request.input, output_mode)
        _log("Ruby", f"共识别 {len(findings)} 条注音，解析后文本: {text}")

        text = await asyncio.to_thread(apply_replace_rules, text, output_mode)
        _log("替换", f"词库替换后文本: {text}")
        text = await asyncio.to_thread(fix_person_suffixes, text, output_mode)
        _log("后缀", f"后缀修正后文本: {text}")

    _log("预览", f"最终文本: {text}")
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
    
    # 尝试触发 uvicorn 的优雅退出
    _server_instance.should_exit = True

    # ★ 核心修复：开一个强制重启线程
    # 如果 uvicorn 因为长连接卡死，这个线程会在 0.8 秒后直接“掀桌子”，强制替换当前进程
    def _force_restart():
        time.sleep(0.8)
        print("[重启] 优雅退出超时，正在强制重载代码...")
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
        
    import threading
    threading.Thread(target=_force_restart, daemon=False).start()


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
        config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info", timeout_graceful_shutdown=3)
        _server_instance = uvicorn.Server(config)
        try:
            _server_instance.run()
        except KeyboardInterrupt:
            break

        if _restart_requested:
            time.sleep(0.5)
            print("[重启] 发现代码更新，正在彻底重启 Python 进程...")
            print()
            # ★ 核心修复：使用 os.execv 直接替换当前进程，并注入 --skip-browser 参数阻止重开窗口
            os.execv(sys.executable, [sys.executable] + sys.argv)
        break