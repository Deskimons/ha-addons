import array
import io
import logging
import os
import signal
import subprocess
import threading
import time
import wave
from typing import Callable, List, Optional, Union
from urllib.parse import urlparse, urlunparse

_LOGGER = logging.getLogger(__name__)

STREAM_CHUNK_SIZE = 4096
STREAM_BUFFER_AHEAD = 0.3


class PlayerState:
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"


class ESP32Player:

    def __init__(self, relay, ha_url: Optional[str] = None) -> None:
        from .esp32_relay import ESP32Relay

        self._relay: ESP32Relay = relay
        self._ha_url = ha_url.rstrip("/") if ha_url else None
        self._state = PlayerState.IDLE
        self._done_callback: Optional[Callable[[], None]] = None
        self._playlist: List[str] = []
        self._volume: float = 100.0
        self._duck_factor: float = 1.0
        self._stop_event = threading.Event()
        self._stream_proc: Optional[subprocess.Popen] = None
        self._paused = False
        self._pcm_cache: dict = {}

    def play(
        self,
        url: Union[str, List[str]],
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = False,
    ) -> None:
        self._stop_internal(flush=False)

        urls = [url] if isinstance(url, str) else list(url)
        if not urls:
            return

        self._done_callback = done_callback
        self._playlist = urls
        self._play_next()

    def pause(self) -> None:
        proc = self._stream_proc
        if proc and not self._paused:
            self._paused = True
            try:
                os.kill(proc.pid, signal.SIGSTOP)
            except OSError:
                pass
            self._relay.send_flush()
            self._state = PlayerState.PAUSED

    def resume(self) -> None:
        proc = self._stream_proc
        if proc and self._paused:
            self._paused = False
            self._relay.send_stream_start()
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except OSError:
                pass
            self._state = PlayerState.PLAYING

    def stop(self) -> None:
        self._stop_internal(flush=True)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(100.0, volume))

    def duck(self) -> None:
        self._duck_factor = 0.3

    def unduck(self) -> None:
        self._duck_factor = 1.0

    def _play_next(self) -> None:
        if not self._playlist:
            self._state = PlayerState.IDLE
            cb = self._done_callback
            self._done_callback = None
            if cb:
                try:
                    cb()
                except Exception:
                    _LOGGER.exception("Error in done_callback")
            return

        url = self._playlist.pop(0)
        url = self._rewrite_url(url)
        _LOGGER.info("[player] Playing: %s", url)
        self._state = PlayerState.LOADING
        self._stop_event.clear()

        thread = threading.Thread(
            target=self._fetch_and_send, args=(url,), daemon=True
        )
        thread.start()

    def _fetch_and_send(self, url: str) -> None:
        try:
            if url.startswith("/") or url.startswith("file://"):
                self._play_local_file(url)
                return
            if url.startswith("http"):
                parsed = urlparse(url)
                if parsed.path.startswith("/api/"):
                    self._play_ha_api_url(url)
                else:
                    self._stream_with_ffmpeg(url)
                return
        except Exception:
            _LOGGER.exception("[player] Error playing %s", url)
            self._state = PlayerState.ERROR
            self._play_next()

    def _play_local_file(self, url: str) -> None:
        path = url.removeprefix("file://")
        pcm = self._pcm_cache.get(path)
        if pcm is None:
            with open(path, "rb") as f:
                raw = f.read()
            pcm = self._decode_to_16k_mono(raw)
            if pcm is not None:
                self._pcm_cache[path] = pcm
        if pcm and not self._stop_event.is_set():
            self._state = PlayerState.PLAYING
            self._relay.send_audio(self._apply_volume(pcm))
        self._state = PlayerState.IDLE
        if not self._stop_event.is_set():
            self._play_next()

    def _play_ha_api_url(self, url: str) -> None:
        curl_cmd = ["curl", "-sL", "--max-time", "15"]
        if "authSig" not in url:
            token = os.environ.get("SUPERVISOR_TOKEN")
            if token:
                curl_cmd += ["-H", f"Authorization: Bearer {token}"]
        curl_cmd.append(url)

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-ar", "16000", "-ac", "1", "-",
        ]

        curl_proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdin=curl_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        curl_proc.stdout.close()

        pcm = ffmpeg_proc.stdout.read()
        ffmpeg_proc.wait(timeout=10)
        curl_proc.wait(timeout=5)

        if pcm and not self._stop_event.is_set():
            _LOGGER.info("[player] HA API: decoded %d bytes PCM", len(pcm))
            self._state = PlayerState.PLAYING
            self._relay.send_audio(self._apply_volume(pcm))
        else:
            stderr = ffmpeg_proc.stderr.read().decode(errors="replace")
            if stderr:
                _LOGGER.warning("[player] ffmpeg stderr: %s", stderr.strip())
        self._state = PlayerState.IDLE
        if not self._stop_event.is_set():
            self._play_next()

    def _stream_with_ffmpeg(self, url: str) -> None:
        try:
            proc = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", url,
                    "-f", "s16le", "-ar", "16000", "-ac", "1", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._stream_proc = proc
            self._relay.send_stream_start()
            self._state = PlayerState.PLAYING

            t0 = time.monotonic()
            bytes_sent = 0

            while not self._stop_event.is_set():
                chunk = proc.stdout.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                bytes_sent += len(chunk)
                audio_time = bytes_sent / 32000.0
                ahead = audio_time - (time.monotonic() - t0)
                if ahead > STREAM_BUFFER_AHEAD:
                    if self._stop_event.wait(timeout=ahead - STREAM_BUFFER_AHEAD):
                        break
                self._relay.send_stream_chunk(self._apply_volume(chunk))
        except Exception:
            _LOGGER.exception("[player] ffmpeg streaming error")
        finally:
            self._relay.send_stream_stop()
            self._cleanup_stream_proc()
            self._state = PlayerState.IDLE
            if not self._stop_event.is_set():
                self._play_next()

    def _cleanup_stream_proc(self) -> None:
        proc = self._stream_proc
        self._stream_proc = None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass

    def _stop_internal(self, flush: bool = True) -> None:
        self._stop_event.set()
        self._playlist.clear()
        if self._paused:
            proc = self._stream_proc
            if proc:
                try:
                    os.kill(proc.pid, signal.SIGCONT)
                except OSError:
                    pass
            self._paused = False
        self._cleanup_stream_proc()
        if flush:
            self._relay.send_flush()

    def _apply_volume(self, pcm: bytes) -> bytes:
        factor = (self._volume / 100.0) * self._duck_factor
        if factor >= 0.99:
            return pcm
        samples = array.array("h", pcm)
        for i in range(len(samples)):
            samples[i] = max(-32768, min(32767, int(samples[i] * factor)))
        return samples.tobytes()

    def _rewrite_url(self, url: str) -> str:
        if not self._ha_url or not url.startswith("http"):
            return url
        parsed = urlparse(url)
        if not parsed.path.startswith("/api/"):
            return url
        target = urlparse(self._ha_url)
        rewritten = parsed._replace(scheme=target.scheme, netloc=target.netloc)
        return urlunparse(rewritten)

    def _decode_to_16k_mono(self, data: bytes) -> Optional[bytes]:
        pcm = self._try_decode_wav(data)
        if pcm is not None:
            return pcm
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, err = proc.communicate(data, timeout=10)
            if proc.returncode == 0 and out:
                return out
            if err:
                _LOGGER.warning("[player] ffmpeg decode: %s", err.decode(errors='replace').strip())
            return None
        except Exception:
            _LOGGER.exception("[player] Failed to decode audio via ffmpeg")
            return None

    @staticmethod
    def _try_decode_wav(data: bytes) -> Optional[bytes]:
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        try:
            with wave.open(io.BytesIO(data), "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
        except Exception:
            return None

        if sample_width != 2:
            return None

        if channels == 2:
            samples = array.array("h", frames)
            mono = array.array("h", [samples[i] for i in range(0, len(samples), 2)])
            frames = mono.tobytes()

        if sample_rate != 16000:
            frames = ESP32Player._resample(frames, sample_rate, 16000)

        return frames

    @staticmethod
    def _resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
        samples = array.array("h", pcm)
        ratio = src_rate / dst_rate
        out_len = int(len(samples) / ratio)
        out = array.array("h", [0] * out_len)
        for i in range(out_len):
            src_pos = i * ratio
            i0 = int(src_pos)
            i1 = min(i0 + 1, len(samples) - 1)
            frac = src_pos - i0
            val = samples[i0] * (1 - frac) + samples[i1] * frac
            out[i] = max(-32768, min(32767, int(val)))
        return out.tobytes()
