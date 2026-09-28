import os
from pathlib import Path

import numpy as np
import pytest

from speak_mcp.errors import ConfigError
from speak_mcp.synth import (
    MODEL,
    MODEL_SIZES,
    VOICES_FILE,
    KokoroSynth,
    Settings,
    encode_ogg,
    encode_wav,
    ensure_models,
    lang_for,
    resolve_espeak,
)

# -- lang_for ----------------------------------------------------------


@pytest.mark.parametrize("voice,lang", [
    ("pf_dora", "pt-br"), ("af_heart", "en-us"), ("bf_alice", "en-gb"),
    ("ef_x", "es"), ("ff_x", "fr-fr"), ("if_x", "it"), ("jf_x", "ja"),
    ("zf_x", "cmn"), ("hf_x", "hi"),
])
def test_lang_for_all_prefixes(voice, lang):
    assert lang_for(voice) == lang


def test_lang_for_unknown_prefix_is_config_error():
    with pytest.raises(ConfigError):
        lang_for("qf_unknown")


# -- Settings ------------------------------------------------------------


def test_settings_defaults(monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) != "/opt/speak")
    s = Settings.from_env({})
    assert s.voice == "pf_dora" and s.speed == 1.0
    assert s.home == Path.home() / ".cache" / "speak-mcp"
    assert s.model_path == s.home / "models" / MODEL
    assert s.voices_path == s.home / "models" / VOICES_FILE


def test_settings_home_prefers_opt_speak_when_present(monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/opt/speak")
    s = Settings.from_env({})
    assert s.home == Path("/opt/speak")


def test_settings_home_env_override_wins(monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    s = Settings.from_env({"SPEAK_HOME": "/custom/home"})
    assert s.home == Path("/custom/home")


def test_settings_from_env_values():
    s = Settings.from_env({"SPEAK_HOME": "/x", "SPEAK_VOICE": "af_heart", "SPEAK_SPEED": "1.2"})
    assert (s.home, s.voice, s.speed) == (Path("/x"), "af_heart", 1.2)


@pytest.mark.parametrize("env", [
    {"SPEAK_VOICE": "qf_unknown"},
    {"SPEAK_SPEED": "3"},
    {"SPEAK_SPEED": "0.5"},
    {"SPEAK_SPEED": "fast"},
])
def test_settings_rejects_bad_values(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


# -- resolve_espeak ---------------------------------------------------------


def test_resolve_espeak_env_override(tmp_path):
    lib = tmp_path / "libespeak-ng.so"
    lib.write_bytes(b"")
    data = tmp_path / "espeak-ng-data"
    data.mkdir()
    assert resolve_espeak({"SPEAK_ESPEAK_LIB": str(lib), "SPEAK_ESPEAK_DATA": str(data)}) == (str(lib), str(data))


def test_resolve_espeak_rejects_bad_data_override(tmp_path):
    lib = tmp_path / "libespeak-ng.so"
    lib.write_bytes(b"")
    with pytest.raises(ConfigError) as exc:
        resolve_espeak({"SPEAK_ESPEAK_LIB": str(lib), "SPEAK_ESPEAK_DATA": "/nonexistent/espeak-ng-data"})
    assert "espeak-ng" in str(exc.value)


def test_resolve_espeak_rejects_bad_lib_path_override(tmp_path):
    data = tmp_path / "espeak-ng-data"
    data.mkdir()
    with pytest.raises(ConfigError) as exc:
        resolve_espeak({"SPEAK_ESPEAK_LIB": "/nonexistent/libespeak-ng.so", "SPEAK_ESPEAK_DATA": str(data)})
    assert "espeak-ng" in str(exc.value)


def test_resolve_espeak_not_found_is_config_error(monkeypatch, tmp_path):
    monkeypatch.setattr("ctypes.util.find_library", lambda name: None)
    monkeypatch.setattr("speak_mcp.synth.ESPEAK_DATA_DIRS", ())
    monkeypatch.setattr("speak_mcp.synth.Path.exists", lambda self: False)
    with pytest.raises(ConfigError):
        resolve_espeak({})


# -- encode_ogg / encode_wav -------------------------------------------------


def _tone(sr=24000, seconds=1.0, freq=440.0):
    return (0.2 * np.sin(2 * np.pi * freq * np.arange(int(sr * seconds)) / sr)).astype(np.float32)


def test_encode_ogg_is_opus_in_ogg():
    data = encode_ogg(_tone(), 24000)
    assert data[:4] == b"OggS" and b"OpusHead" in data[:200]


def test_encode_wav_has_riff_header():
    data = encode_wav(_tone(), 24000)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"


# -- ensure_models -----------------------------------------------------------


def test_ensure_models_downloads_missing_files(tmp_path):
    home = tmp_path
    settings = Settings.from_env({"SPEAK_HOME": str(home)})
    calls = []

    def fake_fetch(url, dest_path):
        calls.append(url)
        dest_path.write_bytes(b"x" * MODEL_SIZES[dest_path.name.removesuffix(".part")])

    ensure_models(settings, fetch=fake_fetch)

    assert settings.model_path.is_file() and settings.model_path.stat().st_size == MODEL_SIZES[MODEL]
    assert settings.voices_path.is_file() and settings.voices_path.stat().st_size == MODEL_SIZES[VOICES_FILE]
    assert len(calls) == 2
    assert not list(home.rglob("*.part"))


def test_ensure_models_redownloads_wrong_size_file(tmp_path):
    home = tmp_path
    settings = Settings.from_env({"SPEAK_HOME": str(home)})
    settings.model_path.parent.mkdir(parents=True)
    settings.model_path.write_bytes(b"short")
    settings.voices_path.write_bytes(b"x" * MODEL_SIZES[VOICES_FILE])

    calls = []

    def fake_fetch(url, dest_path):
        calls.append(dest_path.name)
        dest_path.write_bytes(b"x" * MODEL_SIZES[dest_path.name.removesuffix(".part")])

    ensure_models(settings, fetch=fake_fetch)

    assert calls == [f"{MODEL}.part"]
    assert settings.model_path.stat().st_size == MODEL_SIZES[MODEL]
    assert not list(home.rglob("*.part"))


def test_ensure_models_leaves_no_part_file_on_success(tmp_path):
    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path)})

    def fake_fetch(url, dest_path):
        dest_path.write_bytes(b"x" * MODEL_SIZES[dest_path.name.removesuffix(".part")])

    ensure_models(settings, fetch=fake_fetch)
    assert not list(tmp_path.rglob("*.part"))


def test_ensure_models_raises_config_error_on_short_download(tmp_path):
    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path)})

    def fake_fetch(url, dest_path):
        dest_path.write_bytes(b"too short")

    with pytest.raises(ConfigError):
        ensure_models(settings, fetch=fake_fetch)
    assert not list(tmp_path.rglob("*.part"))


def test_ensure_models_skips_files_already_correct_size(tmp_path):
    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path)})
    settings.model_path.parent.mkdir(parents=True)
    settings.model_path.write_bytes(b"x" * MODEL_SIZES[MODEL])
    settings.voices_path.write_bytes(b"x" * MODEL_SIZES[VOICES_FILE])

    def fake_fetch(url, dest_path):
        raise AssertionError("should not be called when files already match")

    ensure_models(settings, fetch=fake_fetch)


# -- KokoroSynth ---------------------------------------------------------


def test_missing_model_is_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        KokoroSynth(Settings.from_env({"SPEAK_HOME": str(tmp_path)}))
    assert MODEL in str(exc.value)


def test_kokoro_init_wraps_exception(tmp_path, monkeypatch):
    model_file = tmp_path / "models" / MODEL
    voices_file = tmp_path / "models" / VOICES_FILE
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"dummy")
    voices_file.write_bytes(b"dummy")

    monkeypatch.setattr("speak_mcp.synth.resolve_espeak",
                         lambda *a, **kw: (str(tmp_path / "libespeak-ng.so"), str(tmp_path)))

    def mock_kokoro(*args, **kwargs):
        raise RuntimeError("Failed to load espeak-ng from fallback location")

    monkeypatch.setattr("kokoro_onnx.Kokoro", mock_kokoro)

    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path)})
    with pytest.raises(ConfigError) as exc:
        KokoroSynth(settings)
    assert "kokoro init failed" in str(exc.value)


def test_synthesize_unknown_voice_is_config_error(tmp_path, monkeypatch):
    model_file = tmp_path / "models" / MODEL
    voices_file = tmp_path / "models" / VOICES_FILE
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"dummy")
    voices_file.write_bytes(b"dummy")

    monkeypatch.setattr("speak_mcp.synth.resolve_espeak",
                         lambda *a, **kw: (str(tmp_path / "libespeak-ng.so"), str(tmp_path)))

    class FakeKokoro:
        def __init__(self, *a, **kw):
            pass

        def get_voices(self):
            return ["pf_dora"]

        def create(self, *a, **kw):
            raise AssertionError("should not synthesize an unknown voice")

    monkeypatch.setattr("kokoro_onnx.Kokoro", FakeKokoro)

    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path)})
    synth = KokoroSynth(settings)
    with pytest.raises(ConfigError):
        synth.synthesize("olá", voice="zz_unknown")


def test_synthesize_defaults_voice_to_settings_voice(tmp_path, monkeypatch):
    model_file = tmp_path / "models" / MODEL
    voices_file = tmp_path / "models" / VOICES_FILE
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"dummy")
    voices_file.write_bytes(b"dummy")

    monkeypatch.setattr("speak_mcp.synth.resolve_espeak",
                         lambda *a, **kw: (str(tmp_path / "libespeak-ng.so"), str(tmp_path)))

    seen = {}

    class FakeKokoro:
        def __init__(self, *a, **kw):
            pass

        def get_voices(self):
            return ["pf_dora"]

        def create(self, text, voice, speed, lang):
            seen["voice"] = voice
            seen["lang"] = lang
            return np.zeros(10, dtype=np.float32), 24000

    monkeypatch.setattr("kokoro_onnx.Kokoro", FakeKokoro)

    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path), "SPEAK_VOICE": "pf_dora"})
    synth = KokoroSynth(settings)
    samples, sr = synth.synthesize("olá")
    assert seen == {"voice": "pf_dora", "lang": "pt-br"}
    assert sr == 24000 and isinstance(samples, np.ndarray)


def test_voices_delegates_to_kokoro(tmp_path, monkeypatch):
    model_file = tmp_path / "models" / MODEL
    voices_file = tmp_path / "models" / VOICES_FILE
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"dummy")
    voices_file.write_bytes(b"dummy")

    monkeypatch.setattr("speak_mcp.synth.resolve_espeak",
                         lambda *a, **kw: (str(tmp_path / "libespeak-ng.so"), str(tmp_path)))

    class FakeKokoro:
        def __init__(self, *a, **kw):
            pass

        def get_voices(self):
            return ["pf_dora", "pm_alex"]

    monkeypatch.setattr("kokoro_onnx.Kokoro", FakeKokoro)

    settings = Settings.from_env({"SPEAK_HOME": str(tmp_path)})
    synth = KokoroSynth(settings)
    assert synth.voices() == ["pf_dora", "pm_alex"]


# Real synthesis: runs only where the model is present (dev Mac via SPEAK_HOME, CI smoke job).
REAL_HOME = os.environ.get("SPEAK_HOME", "/opt/speak")


@pytest.mark.skipif(not Path(REAL_HOME, "models", MODEL).exists(),
                     reason="Kokoro model not installed")
def test_real_synthesis_pt_br():
    synth = KokoroSynth(Settings.from_env(os.environ))
    samples, sr = synth.synthesize("Olá, este é um teste curto do speak em áudio. A, N ou D?")
    assert sr == 24000
    seconds = len(samples) / sr
    assert 2 < seconds < 15
