# -*- coding: utf-8 -*-
"""桌宠AI · Ollama 协议适配器（桥接形象侧引擎 → 桌宠大脑 8094）。

形象侧引擎（如 dsh-pet 0.3.0）的"本机模型"只说 Ollama 协议（/api/tags、/api/chat），
桌宠大脑在 8094 说 OpenAI 协议（/v1/chat/completions）。本服务在 11434 起
一个翻译层：形象侧的碎碎念/对话全部转发给桌宠本体，回话再翻回 Ollama 格式。

零外部依赖（urllib + FastAPI 同款栈太重，这里用 http.server 手写）。
跑法：python app/tools/ollama_adapter.py（后台，:11434）
"""
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRAIN_BASE = "http://127.0.0.1:8094/v1"
MODEL = "pet"


def _to_openai(ollama_body):
    msgs = []
    for m in ollama_body.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):                 # 多模态（图片）转 OpenAI 格式
            parts = []
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append({"type": "text", "text": c.get("text", "")})
                elif isinstance(c, dict) and c.get("images"):
                    for img in c["images"]:
                        parts.append({"type": "image_url",
                                      "image_url": {"url": "data:image/png;base64," + img}})
            msgs.append({"role": role, "content": parts})
        else:
            msgs.append({"role": role, "content": content})
    return {"model": MODEL, "messages": msgs, "stream": False,
            "temperature": ollama_body.get("options", {}).get("temperature", 0.8)}


def _call_brain(openai_body):
    req = urllib.request.Request(
        BRAIN_BASE + "/chat/completions",
        data=json.dumps(openai_body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    msg = d["choices"][0]["message"]
    return msg.get("content", ""), msg.get("images") or []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/version"):
            # Ollama 客户端（含 openai 兼容探活）会先读它；缺了会 404 被当成服务不存在
            self._json(200, {"version": "0.1.0"})
        elif self.path.startswith("/api/tags"):
            # 形象侧用它判断"本机模型在不在"——恒报可用，模型名随意
            self._json(200, {"models": [{"name": MODEL, "model": MODEL,
                                         "size": 1, "details": {"family": "desktop-pet"}}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"error": "bad json"})
        if self.path.startswith("/api/chat"):
            try:
                t0 = time.time()
                content, _imgs = _call_brain(_to_openai(body))
                print(f"[ollama-adapter] chat {time.time()-t0:.1f}s → {content[:60]}",
                      flush=True)
                self._json(200, {"model": MODEL, "created_at": time.strftime("%FT%TZ"),
                                 "message": {"role": "assistant", "content": content},
                                 "done": True})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print("[ollama-adapter] 127.0.0.1:11434 → 桌宠 8094（Ollama 协议翻译层）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 11434), Handler).serve_forever()
