# -*- coding: utf-8 -*-
"""ATXP 2API - 将 chat.atxp.ai 账号转换为 OpenAI 兼容 API

两步聊天流程：
  1. POST /api/agents/chat/ATXP → {streamId, conversationId, status}
  2. GET /api/agents/chat/stream/{conversationId} → SSE 流式响应

token 管理：
  - refreshToken → POST /api/auth/refresh → access_token（15分钟）
  - refreshToken 每次刷新后轮换（旧的失效），需保存新的

ATXP 端点仅支持 Anthropic 模型（claude-sonnet-4-6、claude-opus-4-6 等），
其他模型（GPT、Gemini）会返回 "Invalid model spec"。

用法:
    python api_server.py                          # 默认配置
    python api_server.py -p 8080                  # 指定端口
    python api_server.py -a accounts.json         # 指定账号文件
    python api_server.py --api-key sk-mykey123    # 设置 API key
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass

import aiohttp
from aiohttp import web

sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))],
)
logger = logging.getLogger("api_server")

BASE_URL = "https://chat.atxp.ai"
TOKEN_TTL = 900 - 60  # access token 有效期，提前 60 秒刷新
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


@dataclass
class Account:
    """单个账号"""
    email: str
    refresh_token: str
    access_token: str = ""
    token_expires: float = 0
    in_use: bool = False
    error_count: int = 0
    last_error: str = ""


class AccountPool:
    """账号池：轮询、token 刷新、token 轮换"""

    def __init__(self, accounts_file: str):
        self._accounts: list[Account] = []
        self._accounts_file = accounts_file
        self._index = 0
        self._lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._http: aiohttp.ClientSession | None = None

    async def start(self, http: aiohttp.ClientSession):
        self._http = http

    def load(self) -> int:
        """从 JSON 文件加载账号"""
        with open(self._accounts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]

        for item in data:
            rt = (item.get("refresh_token", "")
                  or item.get("cookie_dict", {}).get("refreshToken", "")
                  or item.get("key_cookies", {}).get("refreshToken", ""))
            if not rt:
                logger.warning("跳过无 refreshToken: %s", item.get("email", "?"))
                continue
            self._accounts.append(Account(email=item.get("email", "?"), refresh_token=rt))

        logger.info("已加载 %d 个账号", len(self._accounts))
        return len(self._accounts)

    def _save(self):
        """将当前 refreshToken 保存回文件"""
        data = [{"email": a.email, "refresh_token": a.refresh_token} for a in self._accounts]
        with open(self._accounts_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    async def acquire(self) -> Account | None:
        """获取一个可用账号"""
        async with self._lock:
            if not self._accounts:
                return None
            start = self._index
            for _ in range(len(self._accounts)):
                acc = self._accounts[self._index]
                self._index = (self._index + 1) % len(self._accounts)
                if acc.error_count < 5 and not acc.in_use:
                    acc.in_use = True
                    return acc
            # 全部忙/失败，强制返回
            acc = self._accounts[start]
            acc.in_use = True
            return acc

    def release(self, acc: Account, error: str = ""):
        acc.in_use = False
        if error:
            acc.error_count += 1
            acc.last_error = error
        else:
            acc.error_count = 0

    async def ensure_token(self, acc: Account) -> str:
        """获取有效的 access token，必要时刷新（含 refreshToken 轮换处理）"""
        if acc.access_token and time.time() < acc.token_expires:
            return acc.access_token

        # 每账号独立锁
        if acc.email not in self._refresh_locks:
            self._refresh_locks[acc.email] = asyncio.Lock()
        async with self._refresh_locks[acc.email]:
            # 双重检查
            if acc.access_token and time.time() < acc.token_expires:
                return acc.access_token

            logger.info("[%s] 刷新 token", acc.email)
            async with self._http.post(
                f"{BASE_URL}/api/auth/refresh",
                headers={
                    "Cookie": f"refreshToken={acc.refresh_token}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                    "User-Agent": UA,
                },
                json={},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"刷新失败 [{resp.status}]: {text[:200]}")

                data = await resp.json(content_type=None)
                token = data.get("token", "")
                if not token:
                    raise RuntimeError(f"无 token: {str(data)[:200]}")

                acc.access_token = token
                acc.token_expires = time.time() + TOKEN_TTL

                # 处理 refreshToken 轮换
                for sc in resp.headers.getall("Set-Cookie", []):
                    if "refreshToken=" in sc:
                        new_rt = sc.split("refreshToken=")[1].split(";")[0]
                        if new_rt != acc.refresh_token:
                            acc.refresh_token = new_rt
                            self._save()
                            logger.info("[%s] refreshToken 已轮换并保存", acc.email)

                return token

    @property
    def status(self) -> dict:
        return {
            "total": len(self._accounts),
            "available": sum(1 for a in self._accounts if a.error_count < 5),
            "accounts": [
                {"email": a.email, "errors": a.error_count, "in_use": a.in_use,
                 "has_token": bool(a.access_token)}
                for a in self._accounts
            ],
        }


# ============================================================
# 工具函数
# ============================================================

def _model_map(model: str) -> str:
    """模型名映射 — 自动添加 anthropic/ 前缀"""
    if "/" in model:
        return model
    if model.startswith("claude-"):
        return f"anthropic/{model}"
    if model.startswith("gemini-"):
        return f"google/{model}"
    # OpenAI 模型不需要前缀
    return model


def _messages_to_text(messages: list[dict]) -> str:
    """OpenAI messages → 纯文本"""
    if not messages:
        return ""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        if role == "system":
            parts.append(f"[System] {content}")
        elif role == "assistant":
            parts.append(f"[Assistant] {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def _extract_delta_text(data: dict) -> str:
    """从 LibreChat agents SSE 事件中提取文本增量

    格式: {"event":"on_message_delta","data":{"delta":{"content":[{"type":"text","text":"..."}]}}}
    """
    if not isinstance(data, dict):
        return ""

    event = data.get("event", "")
    if event == "on_message_delta":
        inner = data.get("data", {})
        delta = inner.get("delta", {})
        for part in delta.get("content", []):
            if part.get("type") == "text":
                return part.get("text", "")
    return ""


def _oai_chunk(chunk_id: str, model: str, content: str = "", finish_reason: str = None) -> bytes:
    """构造 OpenAI SSE chunk"""
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


# ============================================================
# 中间件
# ============================================================

@web.middleware
async def auth_middleware(request: web.Request, handler):
    """API key 认证中间件"""
    api_key = request.app.get("api_key")
    if not api_key:
        return await handler(request)

    # /status 不需要认证
    if request.path == "/status":
        return await handler(request)

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    else:
        token = ""

    if token != api_key:
        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
            status=401,
        )
    return await handler(request)


# ============================================================
# API 路由
# ============================================================

async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """POST /v1/chat/completions"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "Invalid JSON body"}}, status=400)

    messages = body.get("messages", [])
    model = body.get("model", "anthropic/claude-opus-4-6")
    stream = body.get("stream", False)
    lc_model = _model_map(model)
    text = _messages_to_text(messages)

    if not text:
        return web.json_response({"error": {"message": "No messages"}}, status=400)

    pool: AccountPool = request.app["pool"]
    acc = await pool.acquire()
    if not acc:
        return web.json_response({"error": {"message": "No available accounts"}}, status=503)

    try:
        token = await pool.ensure_token(acc)
    except Exception as e:
        pool.release(acc, str(e))
        return web.json_response({"error": {"message": f"Token error: {e}"}}, status=502)

    http: aiohttp.ClientSession = request.app["http"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/c/new",
        "User-Agent": UA,
    }

    # 步骤1: 发起聊天
    lc_payload = {
        "text": text,
        "sender": "User",
        "clientTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "isCreatedByUser": True,
        "parentMessageId": "00000000-0000-0000-0000-000000000000",
        "messageId": str(uuid.uuid4()),
        "error": False,
        "endpoint": "ATXP",
        "endpointType": "custom",
        "model": lc_model,
        "modelLabel": None,
        "spec": lc_model,
        "key": "never",
        "isTemporary": True,
        "isRegenerate": False,
        "isContinued": False,
        "conversationId": None,
        "ephemeralAgent": {
            "mcp": ["sys__clear__sys"],
            "web_search": False,
            "file_search": False,
            "execute_code": False,
            "artifacts": False,
        },
    }

    MAX_RETRIES = 3

    try:
        for attempt in range(MAX_RETRIES):
            async with http.post(
                f"{BASE_URL}/api/agents/chat/ATXP",
                headers=headers,
                json=lc_payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 429:
                    # 并发限制，等待后重试
                    retry_data = await resp.text()
                    logger.warning("[%s] 并发限制 (尝试 %d/%d): %s", acc.email, attempt + 1, MAX_RETRIES, retry_data[:100])
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    pool.release(acc, "concurrent_limit")
                    return web.json_response({"error": {"message": "Server busy, please retry later", "type": "rate_limit"}}, status=429)

                if resp.status != 200:
                    err = await resp.text()
                    pool.release(acc, err[:200])
                    return web.json_response({"error": {"message": f"Chat init failed [{resp.status}]: {err[:200]}"}}, status=502)

                # 检查响应类型：JSON（两步流程） vs SSE（错误/直接流）
                ct = resp.headers.get("content-type", "")
                if "application/json" in ct:
                    init_data = await resp.json(content_type=None)
                    break
                else:
                    # 响应是 SSE — 可能是 "Invalid model spec" 错误
                    sse_text = await resp.text()
                    # 解析 SSE 错误
                    for sse_line in sse_text.split("\n"):
                        sse_line = sse_line.strip()
                        if sse_line.startswith("data:"):
                            try:
                                sse_data = json.loads(sse_line[5:].strip())
                                if sse_data.get("text") == "Invalid model spec":
                                    pool.release(acc)
                                    return web.json_response({"error": {"message": f"Model '{model}' is not available on this endpoint", "type": "invalid_request_error"}}, status=400)
                                if sse_data.get("error"):
                                    err_text = sse_data.get("text", str(sse_data)[:200])
                                    pool.release(acc, err_text)
                                    return web.json_response({"error": {"message": f"Upstream error: {err_text}"}}, status=502)
                            except json.JSONDecodeError:
                                pass
                    pool.release(acc, f"Unexpected SSE: {sse_text[:200]}")
                    return web.json_response({"error": {"message": f"Unexpected response format"}}, status=502)
        else:
            pool.release(acc, "max_retries")
            return web.json_response({"error": {"message": "Max retries exceeded"}}, status=502)
    except Exception as e:
        pool.release(acc, str(e))
        return web.json_response({"error": {"message": f"Chat init error: {e}"}}, status=502)

    conv_id = init_data.get("conversationId", "")
    if not conv_id:
        pool.release(acc, "No conversationId")
        return web.json_response({"error": {"message": "No conversationId in response"}}, status=502)

    logger.info("[%s] 聊天开始: conv=%s model=%s", acc.email, conv_id[:12], lc_model)

    # 步骤2: 获取流式响应
    stream_headers = {k: v for k, v in headers.items() if k != "Content-Type"}

    try:
        async with http.get(
            f"{BASE_URL}/api/agents/chat/stream/{conv_id}",
            headers=stream_headers,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as stream_resp:
            if stream_resp.status != 200:
                err = await stream_resp.text()
                pool.release(acc, f"Stream [{stream_resp.status}]")
                return web.json_response({"error": {"message": f"Stream failed: {err[:200]}"}}, status=502)

            if stream:
                return await _stream_response(request, stream_resp, model, acc, pool)
            else:
                return await _collect_response(stream_resp, model, acc, pool)

    except Exception as e:
        pool.release(acc, str(e))
        return web.json_response({"error": {"message": f"Stream error: {e}"}}, status=502)


async def _stream_response(request: web.Request, stream_resp: aiohttp.ClientResponse,
                           model: str, acc: Account, pool: AccountPool) -> web.StreamResponse:
    """转换 LibreChat SSE → OpenAI SSE 流式响应"""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    )
    await response.prepare(request)

    # 发送 role chunk
    role_chunk = {
        "id": chunk_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

    try:
        buffer = ""
        async for chunk in stream_resp.content.iter_any():
            buffer += chunk.decode("utf-8", errors="replace")

            while "\n\n" in buffer:
                event_block, buffer = buffer.split("\n\n", 1)

                # 提取 data: 行
                for line in event_block.strip().split("\n"):
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        await response.write(_oai_chunk(chunk_id, model, finish_reason="stop"))
                        await response.write(b"data: [DONE]\n\n")
                        pool.release(acc)
                        return response

                    try:
                        data = json.loads(data_str)
                        text = _extract_delta_text(data)
                        if text:
                            await response.write(_oai_chunk(chunk_id, model, content=text))
                    except json.JSONDecodeError:
                        pass

        # 流结束但没收到 [DONE]
        await response.write(_oai_chunk(chunk_id, model, finish_reason="stop"))
        await response.write(b"data: [DONE]\n\n")

    except Exception as e:
        logger.error("[%s] 流传输错误: %s", acc.email, e)

    pool.release(acc)
    return response


async def _collect_response(stream_resp: aiohttp.ClientResponse,
                            model: str, acc: Account, pool: AccountPool) -> web.Response:
    """收集完整的 SSE 响应，转换为 OpenAI 非流式格式"""
    full_content = ""
    buffer = ""

    async for chunk in stream_resp.content.iter_any():
        buffer += chunk.decode("utf-8", errors="replace")

    for line in buffer.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
            text = _extract_delta_text(data)
            if text:
                full_content += text
        except json.JSONDecodeError:
            pass

    pool.release(acc)

    return web.json_response({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": full_content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models — 只返回 ATXP 端点支持的 Anthropic 模型"""
    pool: AccountPool = request.app["pool"]
    acc = await pool.acquire()
    if not acc:
        return web.json_response({"error": {"message": "No accounts"}}, status=503)

    try:
        token = await pool.ensure_token(acc)
        async with request.app["http"].get(
            f"{BASE_URL}/api/models",
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip, deflate", "User-Agent": UA},
        ) as resp:
            models_data = await resp.json(content_type=None)
    except Exception as e:
        pool.release(acc, str(e))
        return web.json_response({"error": {"message": str(e)}}, status=502)

    pool.release(acc)

    model_list = []
    if isinstance(models_data, dict):
        # ATXP 端点仅支持 Anthropic 模型
        for name in models_data.get("anthropic", []):
            mid = f"anthropic/{name}"
            model_list.append({"id": mid, "object": "model", "created": int(time.time()), "owned_by": "anthropic"})

    return web.json_response({"object": "list", "data": model_list})


async def handle_status(request: web.Request) -> web.Response:
    """GET /status"""
    return web.json_response(request.app["pool"].status)


# ============================================================
# 入口
# ============================================================

async def on_startup(app: web.Application):
    app["http"] = aiohttp.ClientSession()
    await app["pool"].start(app["http"])


async def on_cleanup(app: web.Application):
    await app["http"].close()


def create_app(accounts_file: str, api_key: str = "") -> web.Application:
    pool = AccountPool(accounts_file)
    if pool.load() == 0:
        logger.error("无可用账号")
        sys.exit(1)

    app = web.Application(middlewares=[auth_middleware])
    app["pool"] = pool
    app["api_key"] = api_key
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/status", handle_status)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ATXP 2API")
    parser.add_argument("-p", "--port", type=int, default=8741)
    parser.add_argument("-a", "--accounts", default="results/accounts.json")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--api-key", default=os.environ.get("API_KEY", ""),
                        help="API key 认证（留空则无需认证）")
    args = parser.parse_args()

    app = create_app(args.accounts, args.api_key)
    if args.api_key:
        logger.info("API key 认证已启用")
    logger.info("ATXP 2API 启动: %s:%d", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)
