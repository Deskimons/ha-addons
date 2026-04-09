import asyncio
import logging
import socket
from typing import Callable, Optional

_LOGGER = logging.getLogger(__name__)

EMOTIONS = ["neutral", "happy", "sleepy", "thinking", "sad", "listening", "none"]


class ESP32Relay:

    def __init__(self, port: int = 9522) -> None:
        self.port = port
        self._writer: Optional[asyncio.StreamWriter] = None
        self._current_emotion: int = 0
        self._audio_callback: Optional[Callable[[bytes], None]] = None
        self._emotion_callback: Optional[Callable[[str], None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._send_queue: Optional[asyncio.Queue] = None

    def set_audio_callback(self, cb: Callable[[bytes], None]) -> None:
        self._audio_callback = cb

    def set_emotion_callback(self, cb: Callable[[str], None]) -> None:
        self._emotion_callback = cb

    async def start(self, host: str = "0.0.0.0") -> None:
        self._loop = asyncio.get_running_loop()
        self._send_queue = asyncio.Queue()
        asyncio.ensure_future(self._send_loop())
        server = await asyncio.start_server(self._handle_client, host, self.port)
        _LOGGER.info("[relay] TCP relay listening on %s:%d", host, self.port)
        asyncio.ensure_future(server.serve_forever())

    async def _send_loop(self) -> None:
        while True:
            data = await self._send_queue.get()
            writer = self._writer
            if writer is None:
                continue
            try:
                writer.write(data)
                await writer.drain()
            except Exception:
                _LOGGER.exception("[relay] Send error")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        _LOGGER.info("[relay] Deskimon connected from %s", addr)

        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass

        self._writer = writer

        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
        except Exception:
            pass

        self.send_emotion(self._current_emotion)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode("ascii", errors="ignore").strip()

                if cmd.startswith("MIC:"):
                    nbytes = int(cmd[4:])
                    data = b""
                    while len(data) < nbytes:
                        chunk = await reader.read(nbytes - len(data))
                        if not chunk:
                            break
                        data += chunk
                    if self._audio_callback and data:
                        self._audio_callback(data)

                elif cmd.startswith("EMO:"):
                    idx = int(cmd[4:])
                    if 0 <= idx < len(EMOTIONS):
                        name = EMOTIONS[idx]
                        _LOGGER.info("[relay] <- EMO:%d (%s)", idx, name)
                        self._current_emotion = idx
                        if self._emotion_callback:
                            self._emotion_callback(name)
                    else:
                        _LOGGER.warning("[relay] <- EMO:%d out of range", idx)
                else:
                    _LOGGER.debug("[relay] Unknown command: %s", cmd)

        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            _LOGGER.info("[relay] Deskimon disconnected from %s", addr)
            if self._writer is writer:
                self._writer = None
            writer.close()

    def send_emotion(self, index: int) -> None:
        self._current_emotion = index
        self._enqueue(f"EMO:{index}\n".encode())

    def send_audio(self, pcm: bytes) -> None:
        if not self._writer:
            _LOGGER.warning("[relay] send_audio: no writer, dropping %d bytes", len(pcm))
            return
        _LOGGER.info("[relay] -> PLAY:%d bytes", len(pcm))
        header = f"PLAY:{len(pcm)}\n".encode()
        self._enqueue(header + pcm)

    def send_stream_start(self) -> None:
        if not self._writer:
            return
        self._enqueue(b"SSTART\n")

    def send_stream_chunk(self, pcm: bytes) -> None:
        if not self._writer:
            return
        header = f"SCHK:{len(pcm)}\n".encode()
        self._enqueue(header + pcm)

    def send_stream_stop(self) -> None:
        self._enqueue(b"SSTOP\n")

    def send_flush(self) -> None:
        self._enqueue(b"FLUSH\n")

    def _enqueue(self, data: bytes) -> None:
        loop = self._loop
        q = self._send_queue
        if loop is None or q is None:
            return
        loop.call_soon_threadsafe(q.put_nowait, data)

    @property
    def connected(self) -> bool:
        return self._writer is not None
