#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地音乐播放器 - 桌面 exe 入口 (pywebview + WebView2 + 内嵌文件服务 + 目录记忆)"""
import os
import sys
import json
import re
import time
import shutil
import threading
import mimetypes
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote
import webview

try:
    from mutagen import File as MutagenFile
    HAVE_MUTAGEN = True
except Exception:
    HAVE_MUTAGEN = False

CURRENT_BASE = "."

# 状态标记：记录 JS 端是否成功通过 get_library 加载了曲目
STATUS_STATE = {"loaded": 0, "last_dir": None}

AUDIO_EXT = re.compile(r"\.(mp3|flac|m4a|wav|ogg|aac|wma)$", re.I)


def app_dir():
    # 用真实启动路径（onefile 模式下 sys.argv[0] 指向用户实际运行的 exe）
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def config_path():
    return os.path.join(app_dir(), "player_config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(d):
    try:
        os.makedirs(app_dir(), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def resource_path(rel):
    # PyInstaller 打包后资源在 _MEIPASS；开发时在同目录
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _trace(m):
    try:
        with open(os.path.join(app_dir(), "server_trace.log"), "a", encoding="utf-8") as f:
            f.write(m + "\n"); f.flush()
    except Exception:
        pass


# ---------------------------------------------------------------
# MP3 帧级裁剪：按帧边界切割，不重新编码 -> 无音质损失
# ---------------------------------------------------------------
_BITRATES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}
_SAMPLE_RATES = {1: [44100, 48000, 32000, 0], 2: [22050, 24000, 16000, 0], 25: [11025, 12000, 8000, 0]}


def _parse_frame_header(d, pos):
    """解析一帧 MP3 头，返回 (帧字节长度, 帧时长秒) 或 None"""
    if pos + 4 > len(d):
        return None
    b1, b2 = d[pos + 1], d[pos + 2]
    if d[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    ver_bits = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    if ver_bits == 1 or layer_bits == 0:
        return None
    version = {0: 25, 2: 2, 3: 1}[ver_bits]
    layer = {3: 1, 2: 2, 1: 3}[layer_bits]
    br_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    padding = (b2 >> 1) & 0x01
    vg = 1 if version == 1 else 2
    br = _BITRATES[(vg, layer)][br_idx] * 1000
    sr = _SAMPLE_RATES[version][sr_idx]
    if not br or not sr:
        return None
    if layer == 1:
        flen, spf = (12 * br // sr + padding) * 4, 384
    elif layer == 3 and version != 1:
        flen, spf = 72 * br // sr + padding, 576
    else:
        flen, spf = 144 * br // sr + padding, 1152
    if flen < 4:
        return None
    return flen, spf / float(sr)


def _mp3_index(d):
    """返回 (ID3v2 标签结束位置, [(帧偏移, 帧长度, 帧时长), ...])"""
    n = len(d)
    start = 0
    if d[:3] == b"ID3" and n >= 10:
        size = ((d[6] & 0x7F) << 21) | ((d[7] & 0x7F) << 14) | ((d[8] & 0x7F) << 7) | (d[9] & 0x7F)
        start = 10 + size
        if d[5] & 0x10:          # 含 footer
            start += 10
    end = n
    if n >= 128 and d[n - 128:n - 125] == b"TAG":    # ID3v1
        end -= 128
    frames = []
    pos = min(start, n)
    while pos + 4 <= end:
        h = _parse_frame_header(d, pos)
        if not h:
            pos += 1
            continue
        flen, dur = h
        frames.append((pos, flen, dur))
        pos += flen
    return start, frames


def _mp3_cut(data, start_sec, end_sec):
    """按帧裁剪，返回 (新字节, 实际时长)"""
    id3_end, frames = _mp3_index(data)
    if not frames:
        raise ValueError("无法解析 MP3 帧，文件可能损坏")
    tag = data[:id3_end] if id3_end > 0 else b""
    # 首个帧若是 Xing/Info 信息帧（VBR 索引），帧数元数据已失效，直接丢弃
    off0, len0, _ = frames[0]
    if b"Xing" in data[off0:off0 + len0] or b"Info" in data[off0:off0 + len0]:
        frames = frames[1:]
    if not frames:
        raise ValueError("无法解析 MP3 帧，文件可能损坏")
    t, kept, kept_dur = 0.0, [], 0.0
    for off, flen, dur in frames:
        if t + dur > start_sec and t < end_sec:
            kept.append((off, flen))
            kept_dur += dur
        t += dur
    if not kept:
        raise ValueError("选择的片段为空")
    out = bytearray(tag)
    for off, flen in kept:
        out += data[off:off + flen]
    return bytes(out), kept_dur


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def _replace_retry(tmp, target, tries=12):
    """Windows 上目标文件可能被占用，重试替换"""
    last = None
    for _ in range(tries):
        try:
            os.replace(tmp, target)
            return True
        except OSError as e:
            last = e
            time.sleep(0.15)
    raise OSError("无法替换原文件（可能被占用）：%r" % (last,))


def start_server():
    for p in range(8765, 8800):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), FileHandler)
            srv.daemon_threads = True
            _trace("binding port " + str(p))
            def run(s):
                try:
                    s.serve_forever()
                except Exception as e:
                    _trace("serve_forever error on " + str(p) + ": " + repr(e))
            threading.Thread(target=run, args=(srv,), daemon=True).start()
            _trace("server listening on " + str(p))
            return p
        except OSError as e:
            _trace("bind " + str(p) + " failed: " + repr(e))
            continue
    _trace("start_server: no port available")
    return None


class FileHandler(BaseHTTPRequestHandler):
    def _resolve(self, name):
        name = unquote(name)
        base = CURRENT_BASE
        full = os.path.normpath(os.path.join(base, name))
        if not full.startswith(os.path.realpath(base) + os.sep):
            return None
        return full

    def do_GET(self):
        if self.path.startswith("/_js?"):
            self._jslog()
            return
        path = self.path.split("?", 1)[0]      # 忽略 ?v= 缓存参数
        if path == "/_status":
            self._status()
        elif path.startswith("/file/"):
            self._serve(path[len("/file/"):])
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def _jslog(self):
        try:
            msg = self.path.split("?", 1)[1] if "?" in self.path else ""
            _trace("JS: " + msg)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            try:
                self.send_error(500)
            except Exception:
                pass

    def _status(self):
        try:
            body = json.dumps(STATUS_STATE).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            try:
                self.send_error(500)
            except Exception:
                pass

    def _serve(self, name):
        full = self._resolve(name)
        if not full or not os.path.isfile(full):
            self.send_error(404)
            return
        try:
            size = os.path.getsize(full)
            rng = self.headers.get("Range")
            ctype = mimetypes.guess_type(full)[0] or "audio/mpeg"
            if rng and rng.startswith("bytes="):
                parts = rng[6:].split("-")
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                with open(full, "rb") as f:
                    f.seek(start)
                    data = f.read(length)
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            else:
                with open(full, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except Exception:
            try:
                self.send_error(500)
            except Exception:
                pass

    def log_message(self, *args):
        pass


class Api:
    def __init__(self):
        self.dir = None
        self.port = None
        self.cw = 480
        self.ch = 680

    # ---- 窗口控制（无边框） ----
    def move(self, x, y):
        try:
            webview.windows[0].move(int(x), int(y))
        except Exception:
            pass

    def resize(self, w, h):
        try:
            webview.windows[0].resize(int(w), int(h))
            self.cw, self.ch = int(w), int(h)
        except Exception:
            pass

    def get_size(self):
        return [self.cw, self.ch]

    def get_pos(self):
        try:
            return list(webview.windows[0].get_position())
        except Exception:
            return [0, 0]

    def minimize(self):
        try:
            webview.windows[0].minimize()
        except Exception:
            pass

    def close(self):
        try:
            webview.windows[0].destroy()
        except Exception:
            pass

    # ---- 音乐库 / 目录记忆 ----
    def set_dir(self, path):
        global CURRENT_BASE
        CURRENT_BASE = path
        self.dir = path
        cfg = load_config()
        cfg["last_dir"] = path
        save_config(cfg)
        if self.port is None:
            self.port = start_server()

    def scan(self, base):
        try:
            names = sorted([f for f in os.listdir(base) if AUDIO_EXT.search(f)])
        except Exception:
            return []
        out = []
        for f in names:
            dur = None
            if HAVE_MUTAGEN:
                try:
                    m = MutagenFile(os.path.join(base, f))
                    if m and m.info:
                        dur = int(m.info.length)
                except Exception:
                    pass
            out.append({
                "name": f,
                "title": re.sub(r"\.[^.]+$", "", f),
                "url": "http://127.0.0.1:%d/file/%s" % (self.port, quote(f)),
                "duration": dur,
            })
        return out

    def library(self):
        if not self.dir or not os.path.isdir(self.dir):
            return {"dir": None, "tracks": []}
        return {"dir": self.dir, "tracks": self.scan(self.dir)}

    def choose_folder(self):
        try:
            res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            return None
        if res and len(res) and res[0]:
            self.set_dir(res[0])
            return self.library()
        return None

    def get_library(self):
        cfg = load_config()
        vol = cfg.get("volume")
        d = cfg.get("last_dir")
        if d and os.path.isdir(d):
            self.set_dir(d)
            tracks = self.scan(self.dir)
            STATUS_STATE["loaded"] = len(tracks)
            STATUS_STATE["last_dir"] = self.dir
            return {"dir": self.dir, "tracks": tracks,
                    "last_track": cfg.get("last_track"), "volume": vol}
        return {"dir": None, "tracks": [], "volume": vol}

    def remember_track(self, name):
        cfg = load_config()
        cfg["last_track"] = name
        save_config(cfg)

    # ---- 音量记忆 ----
    def set_volume(self, v):
        try:
            f = float(v)
        except Exception:
            return {"ok": False}
        f = max(0.0, min(1.0, f))
        cfg = load_config()
        cfg["volume"] = round(f, 4)
        save_config(cfg)
        _trace("set_volume " + str(cfg["volume"]))
        return {"ok": True, "volume": cfg["volume"]}

    def get_volume(self):
        v = load_config().get("volume")
        return v if isinstance(v, (int, float)) else None

    # ---- 删除歌曲（永久删除本地文件）----
    def delete_track(self, name):
        try:
            if not self.dir or not os.path.isdir(self.dir):
                return {"ok": False, "error": "未加载音乐目录"}
            base = os.path.realpath(self.dir)
            fn = os.path.basename(str(name))          # 只取文件名，防路径穿越
            if not AUDIO_EXT.search(fn):
                return {"ok": False, "error": "不是音频文件"}
            full = os.path.realpath(os.path.join(base, fn))
            if not full.startswith(base + os.sep):
                return {"ok": False, "error": "非法路径"}
            if not os.path.isfile(full):
                return {"ok": False, "error": "文件不存在"}
            os.remove(full)                            # 永久删除
            _trace("deleted file: " + fn)
            try:
                STATUS_STATE["loaded"] = max(0, int(STATUS_STATE.get("loaded") or 0) - 1)
            except Exception:
                pass
            # 若删的正是记忆的上一首，清掉该记忆
            cfg = load_config()
            if cfg.get("last_track") == fn:
                cfg.pop("last_track", None)
                save_config(cfg)
            return {"ok": True, "name": fn}
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    # ---- 裁剪音频（原地覆盖，不重新编码）----
    def _safe_path(self, name):
        if not self.dir or not os.path.isdir(self.dir):
            raise ValueError("未加载音乐目录")
        base = os.path.realpath(self.dir)
        raw = str(name)
        if raw != os.path.basename(raw) or "/" in raw or "\\" in raw:
            raise ValueError("非法文件名")
        fn = raw
        if not AUDIO_EXT.search(fn):
            raise ValueError("不是音频文件")
        full = os.path.realpath(os.path.join(base, fn))
        if not full.startswith(base + os.sep):
            raise ValueError("非法路径")
        if not os.path.isfile(full):
            raise ValueError("文件不存在")
        return fn, full

    def trim_track(self, name, start, end):
        """把 [start, end] 之外的音频剪掉，直接覆盖原文件"""
        tmp = None
        try:
            fn, full = self._safe_path(name)
            start = max(0.0, float(start))
            end = float(end)
            if not (end > start):
                return {"ok": False, "error": "时间范围无效"}
            with open(full, "rb") as f:
                data = f.read()

            ext = os.path.splitext(fn)[1].lower()
            if ext == ".mp3":
                out, dur = _mp3_cut(data, start, end)
            else:
                # 非 MP3：有 ffmpeg 就无损转封装裁剪，否则明确报错
                exe = _ffmpeg()
                if not exe:
                    return {"ok": False, "error": "目前仅支持 MP3 裁剪（未找到 ffmpeg）"}
                tmp = full + ".trimtmp" + ext
                r = subprocess.run(
                    [exe, "-y", "-loglevel", "error", "-ss", "%.3f" % start,
                     "-to", "%.3f" % end, "-i", full, "-c", "copy", tmp],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if r.returncode != 0 or not os.path.exists(tmp):
                    return {"ok": False, "error": "ffmpeg 裁剪失败"}
                with open(tmp, "rb") as f:
                    out = f.read()
                os.remove(tmp)
                tmp = None
                dur = end - start

            if len(out) < 1024:
                return {"ok": False, "error": "生成的片段过小，已取消"}

            tmp = full + ".trimtmp"
            with open(tmp, "wb") as f:
                f.write(out)
            _replace_retry(tmp, full)
            tmp = None
            _trace("trimmed %s -> %.2fs-%.2fs (%.1fs kept, %d bytes)" % (fn, start, end, dur, len(out)))
            return {"ok": True, "name": fn, "duration": round(dur, 3), "size": len(out),
                    "start": round(start, 3), "end": round(end, 3)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def has_ffmpeg(self):
        return bool(_ffmpeg())


def main():
    html_path = resource_path("player.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    api = Api()
    # 启动时若已记忆目录，提前把内嵌文件服务起好，避免依赖前端 JS 时序
    cfg = load_config()
    d = cfg.get("last_dir")
    _trace("main cfg last_dir=" + str(d) + " dir_ok=" + str(bool(d and os.path.isdir(d))))
    if d and os.path.isdir(d):
        try:
            api.set_dir(d)
            _trace("main set_dir done, port=" + str(api.port))
        except Exception as e:
            _trace("main set_dir error: " + repr(e))

    # 把端口注入页面，供前端做启动日志上报
    html = html.replace("__API_PORT__", str(api.port or 0))

    webview.create_window(
        "音乐播放器",
        html=html,
        js_api=api,
        frameless=True,
        width=480,
        height=680,
        min_size=(400, 480),
    )
    webview.start()


if __name__ == "__main__":
    main()
