#!/usr/bin/env python3
"""Tiny HTTP + WebSocket server for Super Kippah Bros.

Runs one shared room and can also serve the game files. It uses only Python's
standard library so it works locally and on simple public hosts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import time
from urllib.parse import unquote, urlsplit


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ROOT = Path(__file__).resolve().parent
INDEX_FILE = "super_kippah_bros_phone.html"
ALLOWED_FILES = {
    INDEX_FILE,
    "super_kippah_icon.svg",
    "super_kippah_manifest.webmanifest",
    "super_kippah_sw.js",
    "super_kippah_bros_phone_qr.png",
}
INDEX_PATHS = {
    "",
    "phone",
    "play",
    "game",
    "index.html",
    "super_kippah_bros_phone",
    "super_kippah_bros_phone.html",
}
clients: dict[asyncio.StreamWriter, str] = {}
latest_states: dict[str, dict] = {}


async def read_http_headers(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    headers: dict[str, str] = {}
    first = await reader.readline()
    if not first.startswith(b"GET"):
        return "/", headers
    parts = first.decode("utf-8", "ignore").split()
    path = parts[1] if len(parts) > 1 else "/"
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("utf-8", "ignore").partition(":")
        headers[name.strip().lower()] = value.strip()
    return path, headers


async def send_http_response(writer: asyncio.StreamWriter, status: str, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
    writer.write(
        (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: {content_type}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8") + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def serve_static(path: str, writer: asyncio.StreamWriter) -> None:
    parsed = urlsplit(path)
    clean = unquote(parsed.path).lstrip("/") or INDEX_FILE
    if clean in INDEX_PATHS:
        clean = INDEX_FILE

    # Public hosts sometimes probe paths such as /favicon.ico or a refreshed
    # app route. Serve the game for page-like paths, but keep missing assets 404.
    if "/" in clean:
        if "." not in clean.rsplit("/", 1)[-1]:
            clean = INDEX_FILE
        else:
            await send_http_response(writer, "404 Not Found", b"Not found")
            return
    elif clean not in ALLOWED_FILES:
        if "." not in clean:
            clean = INDEX_FILE
        else:
            await send_http_response(writer, "404 Not Found", b"Not found")
            return

    if clean not in ALLOWED_FILES:
        await send_http_response(writer, "404 Not Found", b"Not found")
        return
    file_path = ROOT / clean
    if not file_path.exists() or not file_path.is_file():
        await send_http_response(writer, "404 Not Found", b"Not found")
        return
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    await send_http_response(writer, "200 OK", file_path.read_bytes(), content_type)


async def send_frame(writer: asyncio.StreamWriter, payload: str) -> None:
    data = payload.encode("utf-8")
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(len(data))
    elif len(data) < 65536:
        header.extend([126, (len(data) >> 8) & 255, len(data) & 255])
    else:
        header.extend([127, 0, 0, 0, 0, (len(data) >> 24) & 255, (len(data) >> 16) & 255, (len(data) >> 8) & 255, len(data) & 255])
    writer.write(bytes(header) + data)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> str | None:
    head = await reader.readexactly(2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        extra = await reader.readexactly(2)
        length = (extra[0] << 8) | extra[1]
    elif length == 127:
        extra = await reader.readexactly(8)
        length = int.from_bytes(extra, "big")
    mask = await reader.readexactly(4) if masked else b"\0\0\0\0"
    data = await reader.readexactly(length)
    if opcode == 8:
        return None
    if masked:
        data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
    return data.decode("utf-8", "ignore")


async def broadcast(message: dict, skip: asyncio.StreamWriter | None = None) -> None:
    dead: list[asyncio.StreamWriter] = []
    payload = json.dumps(message, separators=(",", ":"))
    for writer in clients:
        if writer is skip:
            continue
        try:
            await send_frame(writer, payload)
        except Exception:
            dead.append(writer)
    for writer in dead:
        clients.pop(writer, None)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    path, headers = await read_http_headers(reader)
    key = headers.get("sec-websocket-key")
    if not key:
        await serve_static(path, writer)
        return
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    await writer.drain()

    player_id = secrets.token_hex(4)
    clients[writer] = player_id
    await send_frame(writer, json.dumps({"type": "hello", "id": player_id}))
    for peer_id, peer_state in latest_states.items():
        if peer_id != player_id:
            await send_frame(writer, json.dumps(peer_state, separators=(",", ":")))
    await broadcast({"type": "join", "id": player_id, "time": time.time()}, skip=writer)
    print(f"joined {player_id} ({len(clients)} players)")

    try:
        while True:
            text = await read_frame(reader)
            if text is None:
                break
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            data["id"] = player_id
            data["time"] = time.time()
            if data.get("type") == "state":
                latest_states[player_id] = dict(data)
            await broadcast(data, skip=writer)
    except Exception:
        pass
    finally:
        clients.pop(writer, None)
        latest_states.pop(player_id, None)
        await broadcast({"type": "leave", "id": player_id, "time": time.time()})
        print(f"left {player_id} ({len(clients)} players)")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print(f"Super Kippah multiplayer server running on ws://{HOST}:{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
