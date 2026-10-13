"""Pinned Qwen3-TTS 1.7B CustomVoice, MLX 4-bit; no alternate TTS backend."""

import hashlib
import io
import json
import os
import platform
import sys
import threading
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from speak_mcp.errors import ConfigError

MANIFEST = json.loads(Path(__file__).with_name("model_manifest.json").read_text())
MODEL = "qwen3-tts-1.7b-customvoice-4bit"
MODEL_ID = MANIFEST["repo"]
MODEL_REVISION = MANIFEST["revision"]
LANGUAGES = {x.lower(): x for x in (
    "Portuguese", "English", "Chinese", "Japanese", "Korean", "German",
    "French", "Russian", "Spanish", "Italian", "Auto",
)}


def require_mlx_platform() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ConfigError("Qwen3-TTS via MLX requires macOS on Apple Silicon (arm64); no fallback backend")


@dataclass(frozen=True)
class Settings:
    home: Path
    voice: str
    speed: float
    language: str = "Portuguese"
    instruct: str = "Speak naturally and clearly in Brazilian Portuguese."

    @property
    def model_path(self) -> Path:
        return self.home / "models" / MODEL

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        voice = (env.get("SPEAK_VOICE") or "serena").lower()
        try:
            speed = float(env.get("SPEAK_SPEED") or "1.0")
        except ValueError:
            raise ConfigError("SPEAK_SPEED must be a number") from None
        if not 0.7 <= speed <= 1.5:
            raise ConfigError("SPEAK_SPEED must be 0.7-1.5")
        language = LANGUAGES.get((env.get("SPEAK_LANGUAGE") or "Portuguese").lower())
        if language is None:
            raise ConfigError(f"unsupported SPEAK_LANGUAGE: {env.get('SPEAK_LANGUAGE')!r}")
        default_instruct = "Speak naturally and clearly in Brazilian Portuguese." if language == "Portuguese" else "Speak naturally and clearly."
        return cls(
            Path(env.get("SPEAK_HOME") or Path.home() / ".cache" / "speak-mcp"),
            voice, speed, language, env.get("SPEAK_INSTRUCT", default_instruct),
        )


def model_installed(settings: Settings) -> bool:
    try:
        marker = json.loads((settings.model_path / ".speak-model.json").read_text())
        return marker == {"repo": MODEL_ID, "revision": MODEL_REVISION} and all(
            (settings.model_path / name).stat().st_size == size
            for name, size in MANIFEST["files"].items()
        )
    except (OSError, ValueError):
        return False


def ensure_models(settings: Settings) -> None:
    require_mlx_platform()
    if model_installed(settings):
        return
    try:
        from huggingface_hub import snapshot_download

        settings.model_path.mkdir(parents=True, exist_ok=True)
        # HF writes resumable .incomplete files before atomic renames. Only this
        # pinned snapshot is fetched; its bundled decoder is not a second TTS model.
        with redirect_stdout(sys.stderr):
            snapshot_download(
                MODEL_ID, revision=MODEL_REVISION, local_dir=settings.model_path,
                allow_patterns=list(MANIFEST["files"]), max_workers=2,
            )
        for name, size in MANIFEST["files"].items():
            path = settings.model_path / name
            if path.stat().st_size != size:
                raise ConfigError(f"incomplete model file: {name}")
            if name in MANIFEST["sha256"]:
                with path.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                if digest != MANIFEST["sha256"][name]:
                    raise ConfigError(f"model checksum mismatch: {name}")
        marker = settings.model_path / ".speak-model.json"
        part = marker.with_suffix(".part")
        part.write_text(json.dumps({"repo": MODEL_ID, "revision": MODEL_REVISION}))
        part.replace(marker)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Qwen3-TTS model setup failed: {exc}") from exc


def encode_ogg(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="OGG", subtype="OPUS")
    return buf.getvalue()


def encode_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV")
    return buf.getvalue()


def _load_model(path: Path):
    from mlx_audio.tts.utils import load_model

    return load_model(path)


class QwenSynth:
    def __init__(self, settings: Settings):
        require_mlx_platform()
        if not model_installed(settings):
            raise ConfigError(f"model missing or incomplete: {settings.model_path}; run speak-mcp --setup")
        self._settings = settings
        # Local playback pipelines across threads; MLX generation must serialize.
        self._lock = threading.Lock()
        try:
            with redirect_stdout(sys.stderr):
                self._model = _load_model(settings.model_path)
            if self._model.tokenizer is None or self._model.speech_tokenizer is None:
                raise ValueError("text tokenizer or speech decoder failed to load")
            if settings.voice not in self.voices():
                raise ConfigError(f"unknown voice: {settings.voice!r}; available: {', '.join(self.voices())}")
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"Qwen3-TTS init failed: {exc}") from exc

    @property
    def default_voice(self) -> str:
        return self._settings.voice

    def voices(self) -> list[str]:
        return [x.lower() for x in self._model.get_supported_speakers()]

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        voice = (voice or self.default_voice).lower()
        if voice not in self.voices():
            raise ConfigError(f"unknown voice: {voice!r}")
        instruct = self._settings.instruct
        if self._settings.speed != 1.0:
            # CustomVoice has no numerical speed control: use a pace instruction.
            instruct += f" Speak at approximately {self._settings.speed:g} times normal pace."
        try:
            with self._lock, redirect_stdout(sys.stderr):
                pieces = []
                sample_rate = 24000
                for result in self._model.generate_custom_voice(
                    text=text, speaker=voice, language=self._settings.language,
                    instruct=instruct, max_tokens=2048, verbose=False,
                    stream=True, streaming_interval=1.0,
                ):
                    audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                    pieces.append(audio)
                    sample_rate = result.sample_rate
                if not pieces:
                    raise ValueError("no audio generated")
                samples = np.concatenate(pieces)
                if samples.size == 0 or not np.isfinite(samples).all() or not np.any(samples):
                    raise ValueError("empty, silent or non-finite audio")
                return samples, sample_rate
        except Exception as exc:
            raise ConfigError(f"Qwen3-TTS synthesis failed: {exc}") from exc
