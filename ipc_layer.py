"""
Unix Domain Socket transport for the discovery sidecar ↔ executioner IPC.

Protocol: 4-byte big-endian length prefix + UTF-8 JSON payload (length-framing).
SOCK_STREAM avoids DGRAM message-size limits and is trivially reconnectable.

Server model: stateless request/response.  Each client connects, receives the
current snapshot, then disconnects.  No persistent connections to manage.

Graceful degradation: DiscoveryClient.fetch() returns None on any error so
callers can fall back to standalone polling without a code branch.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import time
from datetime import datetime, timezone
from typing import Optional

SOCKET_PATH = "/tmp/solana_discovery.sock"

_HDR               = struct.Struct(">I")   # 4-byte big-endian uint32 length prefix
_CONNECT_TIMEOUT_S = 0.5
_READ_TIMEOUT_S    = 2.0
_MAX_PAYLOAD_BYTES = 10 * 1024 * 1024      # 10 MB hard cap — sane snapshots are <1 MB


# ── Frame helpers ─────────────────────────────────────────────────────────────

async def _recv_frame(reader: asyncio.StreamReader) -> bytes:
    hdr = await reader.readexactly(_HDR.size)
    (n,) = _HDR.unpack(hdr)
    if n > _MAX_PAYLOAD_BYTES:
        raise ValueError(f"IPC frame too large: {n} bytes (max {_MAX_PAYLOAD_BYTES})")
    return await reader.readexactly(n)


async def _send_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(_HDR.pack(len(payload)) + payload)
    await writer.drain()


# ── Server ────────────────────────────────────────────────────────────────────

class DiscoveryServer:
    """Holds the latest versioned snapshot and sends it to every connecting client."""

    def __init__(self) -> None:
        self._payload: bytes = b"{}"
        self._version: int   = 0

    @property
    def version(self) -> int:
        return self._version

    def update(self, snapshot: dict) -> None:
        """Replace the in-memory snapshot.  Asyncio is single-threaded so no lock needed."""
        self._version += 1
        snapshot["version"] = self._version
        self._payload = json.dumps(snapshot, separators=(",", ":")).encode()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await asyncio.wait_for(_send_frame(writer, self._payload), timeout=2.0)
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def serve(self) -> None:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        srv = await asyncio.start_unix_server(self._handle, path=SOCKET_PATH)
        # Restrict socket to owner-only so other local users can't read discovery data.
        os.chmod(SOCKET_PATH, 0o600)
        print(f"[Sidecar] IPC server listening on {SOCKET_PATH}", flush=True)
        async with srv:
            await srv.serve_forever()


# ── Client ────────────────────────────────────────────────────────────────────

class DiscoveryClient:
    """Fetches the current snapshot from the sidecar.

    Returns None on any error — the caller must fall back to standalone mode.
    Thread-safe: all methods are async and run in the same event loop as the caller.
    """

    def __init__(self) -> None:
        self._last_version: int = -1

    async def fetch(self) -> Optional[dict]:
        """Connect → receive snapshot → disconnect.  None if sidecar is unreachable."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(SOCKET_PATH),
                timeout=_CONNECT_TIMEOUT_S,
            )
            try:
                raw = await asyncio.wait_for(_recv_frame(reader), timeout=_READ_TIMEOUT_S)
                return json.loads(raw)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        except Exception:
            return None

    async def fetch_if_updated(self) -> Optional[dict]:
        """Returns snapshot only when its version exceeds the last seen version."""
        snap = await self.fetch()
        if snap is None:
            return None
        v = snap.get("version", 0)
        if v <= self._last_version:
            return None
        self._last_version = v
        return snap

    def is_fresh(self, snap: dict, max_age_s: float = 30.0) -> bool:
        """True when the snapshot timestamp is within max_age_s seconds of now."""
        ts_str = snap.get("ts", "")
        if not ts_str:
            return False
        try:
            snap_ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            return (time.time() - snap_ts.timestamp()) < max_age_s
        except Exception:
            return False
