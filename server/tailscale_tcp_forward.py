#!/usr/bin/env python3
"""Tiny TCP forwarder for exposing a loopback-only Hermes dashboard on Tailscale."""
from __future__ import annotations

import asyncio
import os
import signal
import sys

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9119"))
TARGET_HOST = os.environ.get("TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9120"))
ALLOW_CLIENTS = {
    value.strip()
    for value in os.environ.get("ALLOW_CLIENTS", "").split(",")
    if value.strip()
}

async def pipe_raw(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def pipe_http_host_rewrite(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay client->dashboard while rewriting Host on every HTTP request.

    Node/Electron can reuse one TCP connection for multiple API calls. Rewriting
    only the first request leaves later keep-alive requests carrying the Tailscale
    Host header, which trips the Hermes dashboard host guard. For WebSocket
    upgrades, rewrite the handshake then switch to raw byte relay.
    """
    buffer = b""
    try:
        while True:
            while b"\r\n\r\n" not in buffer and len(buffer) < 131072:
                chunk = await reader.read(4096)
                if not chunk:
                    if buffer:
                        writer.write(rewrite_initial_http_bytes(buffer))
                        await writer.drain()
                    return
                buffer += chunk
            if b"\r\n\r\n" not in buffer:
                writer.write(buffer)
                await writer.drain()
                buffer = b""
                continue

            marker = b"\r\n\r\n"
            header_block, rest = buffer.split(marker, 1)
            lowered = header_block.lower()
            content_length = 0
            for line in header_block.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    try:
                        content_length = int(line.split(b":", 1)[1].strip() or b"0")
                    except ValueError:
                        content_length = 0
                    break
            while len(rest) < content_length:
                chunk = await reader.read(content_length - len(rest))
                if not chunk:
                    break
                rest += chunk
            body = rest[:content_length]
            buffer = rest[content_length:]

            out = rewrite_initial_http_bytes(header_block + marker + body)
            writer.write(out)
            await writer.drain()

            if b"upgrade:" in lowered and b"websocket" in lowered:
                if buffer:
                    writer.write(buffer)
                    await writer.drain()
                    buffer = b""
                await pipe_raw(reader, writer)
                return
    except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

def rewrite_initial_http_bytes(data: bytes) -> bytes:
    # The Hermes dashboard rejects Host headers that do not match its loopback bind.
    # Rewrite only the first HTTP request headers; then relay bytes raw so WebSocket
    # upgrades and streaming traffic stay untouched.
    if b"\r\n" not in data or not data[:16].lstrip().split(b" ", 1)[0] in {
        b"GET", b"POST", b"PUT", b"PATCH", b"DELETE", b"OPTIONS", b"HEAD"
    }:
        return data
    marker = b"\r\n\r\n"
    if marker not in data:
        return data
    headers, rest = data.split(marker, 1)
    lines = headers.split(b"\r\n")
    rewritten = []
    replaced = False
    for line in lines:
        lower = line.lower()
        if lower.startswith(b"host:"):
            rewritten.append(f"Host: {TARGET_HOST}:{TARGET_PORT}".encode())
            replaced = True
        elif lower.startswith(b"origin:"):
            # Browsers/Electron use the externally visible Tailscale origin on
            # WS upgrades. Hermes validates Origin against the loopback-bound
            # dashboard host, so normalize HTTP(S) origins with the Host header.
            origin_value = line.split(b":", 1)[1].strip()
            if origin_value.startswith((b"http://", b"https://")):
                scheme = b"https" if origin_value.startswith(b"https://") else b"http"
                rewritten.append(scheme + b"://" + f"{TARGET_HOST}:{TARGET_PORT}".encode())
                rewritten[-1] = b"Origin: " + rewritten[-1]
            else:
                rewritten.append(line)
        else:
            rewritten.append(line)
    if not replaced:
        rewritten.insert(1, f"Host: {TARGET_HOST}:{TARGET_PORT}".encode())
    return b"\r\n".join(rewritten) + marker + rest

async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    peer = client_writer.get_extra_info("peername")
    client_ip = peer[0] if peer else ""
    if ALLOW_CLIENTS and client_ip not in ALLOW_CLIENTS:
        client_writer.close()
        await client_writer.wait_closed()
        return
    try:
        target_reader, target_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except Exception as exc:
        sys.stderr.write(f"target connection failed: {exc}\n")
        sys.stderr.flush()
        client_writer.close()
        await client_writer.wait_closed()
        return

    left = asyncio.create_task(pipe_http_host_rewrite(client_reader, target_writer))
    right = asyncio.create_task(pipe_raw(target_reader, client_writer))
    done, pending = await asyncio.wait({left, right}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"forwarding {sockets} -> {TARGET_HOST}:{TARGET_PORT}", flush=True)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with server:
        await stop.wait()

if __name__ == "__main__":
    asyncio.run(main())
