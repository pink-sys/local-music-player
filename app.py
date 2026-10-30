#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地音乐播放器 - 桌面 exe 入口 (pywebview + WebView2 + 内嵌文件服务 + 目录记忆)"""
import os
import sys
import json
import re
import threading
import mimetypes
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
        _trace("request: " + self.path[:80])
        if self.path == "/_status":
            self._status()
        elif self.path.startswith("/_js?"):
            self._jslog()
        elif self.path.startswith("/file/"):
            self._serve(self.path[len("/file/"):])
        else:
            self.send_error(404)

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
                self.end_headers()
                self.wfile.write(data)
            else:
                with open(full, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", ctype)
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
        d = cfg.get("last_dir")
        if d and os.path.isdir(d):
            self.set_dir(d)
            tracks = self.scan(self.dir)
            STATUS_STATE["loaded"] = len(tracks)
            STATUS_STATE["last_dir"] = self.dir
            return {"dir": self.dir, "tracks": tracks,
                    "last_track": cfg.get("last_track")}
        return {"dir": None, "tracks": []}

    def remember_track(self, name):
        cfg = load_config()
        cfg["last_track"] = name
        save_config(cfg)


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
