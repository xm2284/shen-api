# -*- coding: utf-8 -*-
"""
神 API · shen-api  —— 恶搞用的假 AI 接口
协议兼容 OpenAI / Anthropic，任何请求的回答都是 ascii/ 里随机一份字符画。
零依赖：只用 Python 标准库。
"""
import json, os, random, socketserver, time
from http.server import BaseHTTPRequestHandler, HTTPServer


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True        # Python 3.6 兼容（3.7+ 才有 http.server.ThreadingHTTPServer）

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
ART_DIR = os.path.join(BASE, "ascii")

def load_arts():
    """加载素材，并按 config.weights 给每份算权重（文件名命中 key 即取该权重）"""
    weights = CFG.get("weights", {})
    arts = []
    if os.path.isdir(ART_DIR):
        for f in sorted(os.listdir(ART_DIR)):
            if not f.endswith(".txt"):
                continue
            try:
                text = open(os.path.join(ART_DIR, f), encoding="utf-8").read().strip("\n")
            except Exception:
                continue
            w = float(CFG.get("default_weight", 1))
            for key, val in weights.items():
                if key in f:
                    w = float(val)
                    break
            if w > 0:
                arts.append((f, text, w))
    return arts or [("fallback", "(ascii/ 目录是空的)", 1)]

ARTS = load_arts()

def est_tokens(s):
    return max(1, len(s) // 3)

def compose(art):
    """字符画用 markdown 代码块包住 —— 客户端才会按等宽渲染、保留连续空格；
       尾部文案放代码块外面，正常渲染。"""
    body = art
    if CFG.get("wrap_code_block", True):
        body = "```\n" + art + "\n```"
    tail = CFG.get("tail", "")
    return body + ("\n\n" + tail if tail else "")

def pick():
    """加权随机挑一份"""
    total = sum(a[2] for a in ARTS)
    r = random.uniform(0, total)
    acc = 0.0
    for name, text, w in ARTS:
        acc += w
        if r <= acc:
            return compose(text)
    return compose(ARTS[-1][1])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "shen-api/1.0"

    def log_message(self, fmt, *args):
        if CFG.get("debug"):
            print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- 工具 ----------
    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _err(self, msg, code=400, etype="invalid_request_error"):
        self._send(code, {"error": {"message": msg, "type": etype, "param": None, "code": None}})

    def _authed(self):
        if not CFG.get("require_auth"): return True
        k = CFG.get("api_key") or ""
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "): h = h[7:]
        if not h: h = self.headers.get("x-api-key") or ""
        return h.strip() == k

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _path(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/"): p = p[4:]
        return p

    # ---------- 流式 ----------
    def _chunk_write(self, data):
        self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
        self.wfile.flush()

    def _sse(self, obj):
        self._chunk_write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _end_stream(self):
        self._chunk_write(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _stream_openai(self, model, text, usage=True):
        cid = "chatcmpl-" + os.urandom(12).hex()
        ts = int(time.time())
        delay = float(CFG.get("stream_delay_ms", 10)) / 1000.0
        self._start_stream()
        self._sse({"id": cid, "object": "chat.completion.chunk", "created": ts, "model": model,
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        for line in text.split("\n"):
            self._sse({"id": cid, "object": "chat.completion.chunk", "created": ts, "model": model,
                       "choices": [{"index": 0, "delta": {"content": line + "\n"}, "finish_reason": None}]})
            if delay: time.sleep(delay)
        self._sse({"id": cid, "object": "chat.completion.chunk", "created": ts, "model": model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        if usage:
            self._sse({"id": cid, "object": "chat.completion.chunk", "created": ts, "model": model,
                       "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": est_tokens(text),
                                                "total_tokens": 10 + est_tokens(text)}})
        self._end_stream()

    def _stream_anthropic(self, model, text):
        mid = "msg_" + os.urandom(12).hex()
        delay = float(CFG.get("stream_delay_ms", 10)) / 1000.0
        self._start_stream()
        self._sse({"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant",
                   "model": model, "content": [], "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}})
        self._sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        for line in text.split("\n"):
            self._sse({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": line + "\n"}})
            if delay: time.sleep(delay)
        self._sse({"type": "content_block_stop", "index": 0})
        self._sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": est_tokens(text)}})
        self._sse({"type": "message_stop"})
        self._end_stream()

    # ---------- GET ----------
    def do_GET(self):
        p = self._path()
        if p in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 1700000000, "owned_by": "shen"}
                for m in CFG.get("models", [])]})
        elif p == "/health":
            self._send(200, {"status": "ok", "arts": len(ARTS),
                             "weights": {a[0]: a[2] for a in ARTS}})
        elif p in ("/", "/index.html"):
            self._send(200, {"service": "神 API", "fake": True, "arts": len(ARTS),
                             "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/messages", "/health"]})
        else:
            self._err("Not found: " + p, 404, "not_found_error")

    # ---------- POST ----------
    def do_POST(self):
        p = self._path()
        if not self._authed():
            self._err("Incorrect API key provided.", 401, "authentication_error")
            return
        body = self._read_body()
        model = body.get("model") or (CFG.get("models") or ["shen-1"])[0]
        text = pick()
        stream = bool(body.get("stream"))

        if p in ("/v1/chat/completions", "/v1/completions"):
            if stream:
                self._stream_openai(model, text); return
            self._send(200, {"id": "chatcmpl-" + os.urandom(12).hex(), "object": "chat.completion",
                             "created": int(time.time()), "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": est_tokens(text),
                                       "total_tokens": 10 + est_tokens(text)}})
        elif p == "/v1/responses":
            self._send(200, {"id": "resp_" + os.urandom(8).hex(), "object": "response",
                             "created_at": int(time.time()), "model": model, "status": "completed",
                             "output": [{"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": text}]}],
                             "usage": {"input_tokens": 10, "output_tokens": est_tokens(text),
                                       "total_tokens": 10 + est_tokens(text)}})
        elif p in ("/v1/messages", "/v1/messages/count_tokens"):
            if p.endswith("count_tokens"):
                self._send(200, {"input_tokens": 10}); return
            if stream:
                self._stream_anthropic(model, text); return
            self._send(200, {"id": "msg_" + os.urandom(12).hex(), "type": "message", "role": "assistant",
                             "model": model, "content": [{"type": "text", "text": text}],
                             "stop_reason": "end_turn", "stop_sequence": None,
                             "usage": {"input_tokens": 10, "output_tokens": est_tokens(text)}})
        else:
            self._err("Not found: " + p, 404, "not_found_error")

if __name__ == "__main__":
    host = CFG.get("listen", "127.0.0.1"); port = int(CFG.get("port", 3999))
    print("神 API 启动于 http://%s:%d  （素材 %d 份）" % (host, port, len(ARTS)))
    for _n, _t, _w in ARTS: print("    %-30s 权重 %g" % (_n, _w))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
