import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from queue import Queue
from typing import Dict, List, Optional, Set, Union

import numpy as np
from getmac import get_mac_address  # type: ignore
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
from pyopen_wakeword import OpenWakeWord, OpenWakeWordFeatures

from linux_voice_assistant.models import (
    AvailableWakeWord,
    Preferences,
    ServerState,
    WakeWordType,
)
from linux_voice_assistant.satellite import VoiceSatelliteProtocol
from linux_voice_assistant.util import (
    get_default_interface,
    get_default_ipv4,
    get_esphome_version,
    get_version,
)
from linux_voice_assistant.zeroconf import HomeAssistantZeroconf

from .esp32_player import ESP32Player
from .esp32_relay import ESP32Relay

_LOGGER = logging.getLogger(__name__)
_LVA_DIR = Path(sys.modules["linux_voice_assistant"].__file__).parent
_LVA_ROOT = Path(os.environ.get("LVA_ROOT", "/opt/lva"))
_WAKEWORDS_DIR = _LVA_ROOT / "wakewords"
_SOUNDS_DIR = _LVA_ROOT / "sounds"


def parse_args():
    parser = argparse.ArgumentParser(description="Deskimon Voice Bridge")
    parser.add_argument("--name", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=6053)
    parser.add_argument("--wake-model", default="okay_nabu")
    parser.add_argument("--stop-model", default="stop")
    parser.add_argument("--wake-word-dir", default=[_WAKEWORDS_DIR], action="append")
    parser.add_argument("--download-dir", default=_LVA_ROOT / "local")
    parser.add_argument("--refractory-seconds", type=float, default=2.0)
    parser.add_argument("--wakeup-sound", default=str(_SOUNDS_DIR / "wake_word_triggered.flac"))
    parser.add_argument("--timer-finished-sound", default=str(_SOUNDS_DIR / "timer_finished.flac"))
    parser.add_argument("--processing-sound", default=str(_SOUNDS_DIR / "processing.wav"))
    parser.add_argument("--mute-sound", default=str(_SOUNDS_DIR / "mute_switch_on.flac"))
    parser.add_argument("--unmute-sound", default=str(_SOUNDS_DIR / "mute_switch_off.flac"))
    parser.add_argument("--preferences-file", default=_LVA_ROOT / "preferences.json")
    parser.add_argument("--network-interface", default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--relay-port", type=int, default=9522)
    parser.add_argument("--ha-url", default=None)
    return parser.parse_args()


def process_audio_from_relay(
    state: ServerState,
    relay: ESP32Relay,
    refractory_seconds: float,
):
    wake_words: List[Union[MicroWakeWord, OpenWakeWord]] = []
    micro_features = MicroWakeWordFeatures()
    micro_inputs: List[np.ndarray] = []
    oww_features: Optional[OpenWakeWordFeatures] = None
    oww_inputs: List[np.ndarray] = []
    has_oww = False
    last_active: Optional[float] = None
    audio_buffer = bytearray()
    _LOGGER.info("[audio] MicroWakeWordFeatures initialized")

    def on_mic_data(pcm: bytes):
        audio_buffer.extend(pcm)

    relay.set_audio_callback(on_mic_data)

    try:
        while True:
            if len(audio_buffer) < 2048:
                time.sleep(0.01)
                continue

            chunk_size = min(len(audio_buffer), 2048)
            raw = bytes(audio_buffer[:chunk_size])
            del audio_buffer[:chunk_size]

            if state.satellite is None:
                continue

            if (not wake_words) or (state.wake_words_changed and state.wake_words):
                state.wake_words_changed = False
                wake_words = [ww for ww in state.wake_words.values() if ww.id in state.active_wake_words]

                has_oww = False
                for wake_word in wake_words:
                    if isinstance(wake_word, OpenWakeWord):
                        has_oww = True

                if has_oww and (oww_features is None):
                    oww_features = OpenWakeWordFeatures.from_builtin()

                _LOGGER.info(
                    "[audio] Loaded %d wake words: %s (active: %s, has_oww=%s)",
                    len(wake_words),
                    [ww.id for ww in wake_words],
                    state.active_wake_words,
                    has_oww,
                )

            audio_chunk = raw

            try:
                state.satellite.handle_audio(audio_chunk)

                micro_inputs.clear()
                micro_inputs.extend(micro_features.process_streaming(audio_chunk))

                if has_oww and oww_features is not None:
                    oww_inputs.clear()
                    oww_inputs.extend(oww_features.process_streaming(audio_chunk))

                for wake_word in wake_words:
                    activated = False
                    if isinstance(wake_word, MicroWakeWord):
                        for micro_input in micro_inputs:
                            if wake_word.process_streaming(micro_input):
                                activated = True
                    elif isinstance(wake_word, OpenWakeWord):
                        for oww_input in oww_inputs:
                            for prob in wake_word.process_streaming(oww_input):
                                if prob > 0.5:
                                    activated = True

                    if activated and not state.muted:
                        now = time.monotonic()
                        if (last_active is None) or ((now - last_active) > refractory_seconds):
                            _LOGGER.info("[audio] Wake word detected: %s", wake_word.id)
                            state.satellite.wakeup(wake_word)
                            last_active = now

                stopped = False
                for micro_input in micro_inputs:
                    if state.stop_word.process_streaming(micro_input):
                        stopped = True

                if stopped and (state.stop_word.id in state.active_wake_words) and not state.muted:
                    state.satellite.stop()

            except Exception:
                _LOGGER.exception("Unexpected error handling audio")
    except Exception:
        _LOGGER.exception("Unexpected error processing audio from relay")
        sys.exit(1)


async def main():
    args = parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    if not args.network_interface:
        network_interface = get_default_interface()
        _LOGGER.info("Detected network interface: %s", network_interface)
    else:
        network_interface = args.network_interface

    if not args.host:
        host_ip_address = get_default_ipv4(network_interface)
        _LOGGER.info("Detected IP: %s", host_ip_address)
    else:
        host_ip_address = args.host

    mac_address = get_mac_address(interface=network_interface)
    mac_no_colon = mac_address.replace(":", "").lower()
    device_name = f"lva-{mac_no_colon}"

    if not args.name:
        friendly_name = f"Deskimon - {mac_address.replace(':', '')}"
    else:
        friendly_name = args.name

    version = get_version()
    esphome_version = get_esphome_version()

    args.download_dir = Path(args.download_dir)
    args.download_dir.mkdir(parents=True, exist_ok=True)

    wake_word_dirs = [Path(d) for d in args.wake_word_dir]
    wake_word_dirs.append(args.download_dir / "external_wake_words")
    available_wake_words: Dict[str, AvailableWakeWord] = {}

    for wake_word_dir in wake_word_dirs:
        for model_config_path in wake_word_dir.glob("*.json"):
            model_id = model_config_path.stem
            if model_id == args.stop_model:
                continue
            with open(model_config_path, "r", encoding="utf-8") as f:
                model_config = json.load(f)
            model_type = WakeWordType(model_config["type"])
            if model_type == WakeWordType.OPEN_WAKE_WORD:
                wake_word_path = model_config_path.parent / model_config["model"]
            else:
                wake_word_path = model_config_path
            available_wake_words[model_id] = AvailableWakeWord(
                id=model_id,
                type=WakeWordType(model_type),
                wake_word=model_config["wake_word"],
                trained_languages=model_config.get("trained_languages", []),
                wake_word_path=wake_word_path,
            )

    _LOGGER.debug("Available wake words: %s", list(sorted(available_wake_words.keys())))

    preferences_path = Path(args.preferences_file)
    if preferences_path.exists():
        with open(preferences_path, "r", encoding="utf-8") as f:
            preferences = Preferences(**json.load(f))
    else:
        preferences = Preferences()

    initial_volume = max(0.0, min(1.0, float(preferences.volume if preferences.volume is not None else 1.0)))
    preferences.volume = initial_volume

    active_wake_words: Set[str] = set()
    wake_models: Dict[str, Union[MicroWakeWord, OpenWakeWord]] = {}

    if preferences.active_wake_words:
        for wake_word_id in preferences.active_wake_words:
            wake_word = available_wake_words.get(wake_word_id)
            if wake_word is None:
                _LOGGER.warning("Unrecognized wake word id: %s", wake_word_id)
                continue
            wake_models[wake_word_id] = wake_word.load()
            active_wake_words.add(wake_word_id)

    if not wake_models:
        wake_word_id = args.wake_model
        wake_word = available_wake_words.get(wake_word_id)
        if wake_word is None and available_wake_words:
            wake_word_id = next(iter(available_wake_words))
            wake_word = available_wake_words[wake_word_id]
            _LOGGER.warning("Wake model '%s' not found, using '%s'", args.wake_model, wake_word_id)
        if wake_word is not None:
            wake_models[wake_word_id] = wake_word.load()
            active_wake_words.add(wake_word_id)

    stop_model: Optional[MicroWakeWord] = None
    for wake_word_dir in wake_word_dirs:
        stop_config_path = wake_word_dir / f"{args.stop_model}.json"
        if not stop_config_path.exists():
            continue
        stop_model = MicroWakeWord.from_config(stop_config_path)
        break
    assert stop_model is not None

    relay = ESP32Relay(port=args.relay_port)
    esp32_player = ESP32Player(relay, ha_url=args.ha_url)

    state = ServerState(
        name=device_name,
        friendly_name=friendly_name,
        network_interface=network_interface,
        mac_address=mac_address,
        ip_address=host_ip_address,
        version=version,
        esphome_version=esphome_version,
        audio_queue=Queue(),
        entities=[],
        available_wake_words=available_wake_words,
        wake_words=wake_models,
        active_wake_words=active_wake_words,
        stop_word=stop_model,
        music_player=esp32_player,
        tts_player=esp32_player,
        wakeup_sound=args.wakeup_sound,
        timer_finished_sound=args.timer_finished_sound,
        processing_sound=args.processing_sound,
        mute_sound=args.mute_sound,
        unmute_sound=args.unmute_sound,
        preferences=preferences,
        preferences_path=preferences_path,
        refractory_seconds=args.refractory_seconds,
        download_dir=args.download_dir,
        volume=initial_volume,
    )

    volume_pct = int(round(initial_volume * 100))
    state.music_player.set_volume(volume_pct)
    state.tts_player.set_volume(volume_pct)

    await relay.start()

    loop = asyncio.get_running_loop()
    max_attempts = 15
    attempt = 1
    server = None

    while attempt <= max_attempts:
        try:
            server = await loop.create_server(
                lambda: VoiceSatelliteProtocol(state),
                host=host_ip_address,
                port=args.port,
            )
            break
        except OSError as err:
            message = err.strerror or str(err)
            if attempt < max_attempts:
                _LOGGER.warning("Attempt %d/%d bind failed: %s. Retrying...", attempt, max_attempts, message)
                await asyncio.sleep(1)
                attempt += 1
            else:
                _LOGGER.exception("All %d bind attempts failed", max_attempts)
                sys.exit(1)

    audio_thread = threading.Thread(
        target=process_audio_from_relay,
        args=(state, relay, args.refractory_seconds),
        daemon=True,
    )
    audio_thread.start()

    discovery = HomeAssistantZeroconf(
        port=args.port,
        name=state.name,
        mac_address=state.mac_address,
        host_ip_address=host_ip_address,
    )
    await discovery.register_server()

    _LOGGER.info("Deskimon Voice Bridge started (host=%s, port=%s, relay=%s)", host_ip_address, args.port, args.relay_port)

    try:
        async with server:  # type: ignore
            await server.serve_forever()  # type: ignore
    except KeyboardInterrupt:
        pass

    _LOGGER.debug("Server stopped")


if __name__ == "__main__":
    asyncio.run(main())
