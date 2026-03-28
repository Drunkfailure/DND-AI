"""Ollama HTTP client (chat, stream, embeddings, health)."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

import httpx


def _base(base: str) -> str:
    return base.rstrip("/")


async def pull_model(base: str, name: str) -> dict[str, Any]:
    """Download a model into the local Ollama library (`ollama pull` via HTTP). Can take many minutes."""
    url = f"{_base(base)}/api/pull"
    body: dict[str, Any] = {"name": name, "stream": True}
    last: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=3600.0) as client:
        async with client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise RuntimeError(f"pull {response.status_code}: {text.decode(errors='replace')}")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise RuntimeError(str(chunk["error"]))
                last = chunk
                if chunk.get("status") == "success":
                    break
    return last or {"status": "success"}


async def list_models(base: str) -> list[str]:
    url = f"{_base(base)}/api/tags"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    models = data.get("models") or []
    return [m["name"] for m in models if isinstance(m, dict) and "name" in m]


async def chat_completion(
    base: str,
    model: str,
    temperature: float,
    messages: list[tuple[str, str]],
) -> str:
    url = f"{_base(base)}/api/chat"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": r, "content": c} for r, c in messages],
        "stream": False,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(url, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"chat {r.status_code}: {r.text}")
        data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    msg = data.get("message") or {}
    return str(msg.get("content") or "")


async def chat_completion_stream(
    base: str,
    model: str,
    temperature: float,
    messages: list[tuple[str, str]],
    on_chunk: Callable[[str], None],
) -> str:
    url = f"{_base(base)}/api/chat"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": r, "content": c} for r, c in messages],
        "stream": True,
        "options": {"temperature": temperature},
    }
    full = ""
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise RuntimeError(f"chat stream {response.status_code}: {text.decode()}")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(str(chunk["error"]))
                m = chunk.get("message") or {}
                c = m.get("content") or ""
                if c:
                    full += c
                    on_chunk(c)
    return full


async def embed_text(base: str, model: str, text: str) -> list[float]:
    url = f"{_base(base)}/api/embeddings"
    body = {"model": model, "prompt": text}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"embed {r.status_code}: {r.text}")
        data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    emb = data.get("embedding")
    if not emb:
        raise RuntimeError("no embedding")
    return [float(x) for x in emb]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def bytes_to_f32_vec(b: bytes) -> list[float]:
    import struct

    return list(struct.unpack(f"<{len(b) // 4}f", b))


def f32_vec_to_bytes(v: list[float]) -> bytes:
    import struct

    return struct.pack(f"<{len(v)}f", *v)


async def health(base: str) -> bool:
    url = f"{_base(base)}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            return r.status_code < 400
    except Exception:
        return False


async def chat_completion_stream_lines(
    base: str,
    model: str,
    temperature: float,
    messages: list[tuple[str, str]],
) -> AsyncIterator[str]:
    """Yield content chunks from streaming Ollama chat."""
    url = f"{_base(base)}/api/chat"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": r, "content": c} for r, c in messages],
        "stream": True,
        "options": {"temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", url, json=body) as response:
            if response.status_code >= 400:
                text = await response.aread()
                raise RuntimeError(f"chat stream {response.status_code}: {text.decode()}")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(str(chunk["error"]))
                m = chunk.get("message") or {}
                c = m.get("content") or ""
                if c:
                    yield c
