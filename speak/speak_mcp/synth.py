"""Kokoro TTS with system espeak-ng: settings, model download, synth, OGG/WAV encode."""

import ctypes.util
import io
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from speak_mcp.errors import ConfigError

VOICE_LANG = {
    "p": "pt-br",
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "i": "it",
    "j": "ja",
    "z": "cmn",
    "h": "hi",
}

MODEL = "kokoro-v1.0.int8.onnx"
VOICES_FILE = "voices-v1.0.bin"
MODEL_SIZES = {MODEL: 114119327, VOICES_FILE: 28214398}
MODEL_BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

ESPEAK_DATA_DIRS = (
    "/usr/lib/x86_64-linux-gnu/espeak-ng-data",
    "/usr/lib/aarch64-linux-gnu/espeak-ng-data",
    "/usr/share/espeak-ng-data",
    "/opt/homebrew/share/espeak-ng-data",
    "/usr/local/share/espeak-ng-data",
)


def lang_for(voice: str) -> str:
    prefix = voice[:1]
    try:
        return VOICE_LANG[prefix]
    except KeyError:
        raise ConfigError(f"unknown voice prefix: {voice!r}") from None


@dataclass(frozen=True)
class Settings:
    home: Path
    voice: str
    speed: float

    @property
    def model_path(self) -> Path:
        return self.home / "models" / MODEL

    @property
    def voices_path(self) -> Path:
        return self.home / "models" / VOICES_FILE

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        voice = env.get("SPEAK_VOICE") or "pf_dora"
        lang_for(voice)  # only the prefix is validated here; full voice list is checked at synth time
        try:
            speed = float(env.get("SPEAK_SPEED") or "1.0")
        except ValueError:
            raise ConfigError("SPEAK_SPEED must be a number") from None
        if not 0.7 <= speed <= 1.5:
            raise ConfigError("SPEAK_SPEED must be 0.7-1.5")
        home_env = env.get("SPEAK_HOME")
        if home_env:
            home = Path(home_env)
        elif Path("/opt/speak").is_dir():
            home = Path("/opt/speak")
        else:
            home = Path.home() / ".cache" / "speak-mcp"
        return cls(home, voice, speed)


def resolve_espeak(env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    # Validate explicit SPEAK_ESPEAK_DATA override (if set, must exist)
    data_override = env.get("SPEAK_ESPEAK_DATA")
    if data_override and not Path(data_override).is_dir():
        raise ConfigError(f"SPEAK_ESPEAK_DATA directory not found: {data_override}")

    # Validate explicit SPEAK_ESPEAK_LIB override (if set and is a path, must exist)
    lib_override = env.get("SPEAK_ESPEAK_LIB")
    if lib_override and "/" in lib_override and not Path(lib_override).is_file():
        raise ConfigError(f"SPEAK_ESPEAK_LIB file not found: {lib_override}")

    lib = lib_override or ctypes.util.find_library("espeak-ng")
    if not lib and Path("/opt/homebrew/lib/libespeak-ng.dylib").exists():
        lib = "/opt/homebrew/lib/libespeak-ng.dylib"
    data = data_override or next(
        (d for d in ESPEAK_DATA_DIRS if Path(d).is_dir()), None)
    if not lib or not data:
        raise ConfigError("system espeak-ng not found (apt install espeak-ng / brew install espeak-ng)")
    return lib, data


def _download(url: str, dest_path: Path, *, attempts: int = 3, backoff: float = 0.5) -> None:
    """Fetch `url` into `dest_path`, retrying transient network errors.

    Up to `attempts` tries with a short linear backoff between them; the destination is
    reopened (truncated) on every attempt, so a partial write from a failed try never leaks
    into the next one. ensure_models() still does the real correctness check (.part + exact
    size before renaming into place) -- this only avoids treating one dropped connection as a
    hard failure on a flaky cloud-VM network.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp, open(dest_path, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
            return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(backoff * attempt)
    assert last_exc is not None
    raise last_exc


def ensure_models(settings: Settings, fetch: Callable[[str, Path], None] = _download) -> None:
    try:
        settings.model_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create model directory {settings.model_path.parent}: {exc}") from exc
    for name, size in MODEL_SIZES.items():
        dest = settings.home / "models" / name
        if dest.is_file() and dest.stat().st_size == size:
            continue
        part = dest.with_name(dest.name + ".part")
        url = f"{MODEL_BASE_URL}/{name}"
        try:
            fetch(url, part)
            actual = part.stat().st_size if part.is_file() else 0
            if actual != size:
                raise ConfigError(
                    f"{name} download incomplete: got {actual} bytes, expected {size}")
        except Exception as exc:
            if part.exists():
                part.unlink()
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"failed to download {name}: {exc}") from exc
        part.rename(dest)


def encode_ogg(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="OGG", subtype="OPUS")
    return buf.getvalue()


def encode_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV")
    return buf.getvalue()


class KokoroSynth:
    def __init__(self, settings: Settings):
        for path in (settings.model_path, settings.voices_path):
            if not path.is_file():
                raise ConfigError(f"model file missing: {path}")
        lib, data = resolve_espeak()
        from kokoro_onnx import Kokoro
        from kokoro_onnx.config import EspeakConfig

        self._settings = settings
        try:
            self._kokoro = Kokoro(str(settings.model_path), str(settings.voices_path),
                                  espeak_config=EspeakConfig(lib_path=lib, data_path=data))
        except Exception as exc:
            raise ConfigError(f"kokoro init failed: {exc}") from exc

    @property
    def default_voice(self) -> str:
        """The voice a call falls back to when no explicit `voice` override
        is given (SPEAK_VOICE, or 'pf_dora'). Exposed so callers can validate
        it against the loaded voices file the same way an explicit override
        is validated (regression: M3)."""
        return self._settings.voice

    def voices(self) -> list[str]:
        return self._kokoro.get_voices()

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        voice = voice or self._settings.voice
        if voice not in self._kokoro.get_voices():
            raise ConfigError(f"unknown voice: {voice!r}")
        lang = lang_for(voice)
        samples, sr = self._kokoro.create(text, voice=voice, speed=self._settings.speed, lang=lang)
        return samples, sr
