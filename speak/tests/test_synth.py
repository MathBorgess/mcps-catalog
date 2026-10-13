"""Portable adapter tests; real MLX synthesis is separately opt-in."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from speak_mcp import synth
from speak_mcp.errors import ConfigError
from speak_mcp.synth import Settings, QwenSynth, encode_ogg, encode_wav


def test_defaults():
    s = Settings.from_env({})
    assert s.voice == 'serena' and s.language == 'Portuguese'
    assert s.home == Path.home() / '.cache' / 'speak-mcp'
    assert s.speed == 1.0


@pytest.mark.parametrize('env', [
    {'SPEAK_SPEED': 'fast'}, {'SPEAK_SPEED': 'nan'}, {'SPEAK_SPEED': '3'},
    {'SPEAK_SPEED': '0.5'}, {'SPEAK_LANGUAGE': 'Klingon'},
])
def test_invalid_settings(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_language_independent_of_timbre():
    s = Settings.from_env({'SPEAK_VOICE': 'Ryan', 'SPEAK_LANGUAGE': 'Portuguese'})
    assert s.voice == 'ryan' and s.language == 'Portuguese'
    assert Settings.from_env({'SPEAK_LANGUAGE': 'English'}).instruct == 'Speak naturally and clearly.'


def test_unsupported_platform_fails_before_download(monkeypatch, tmp_path):
    monkeypatch.setattr(synth.sys, 'platform', 'linux')
    with pytest.raises(ConfigError, match='requires macOS'):
        synth.ensure_models(Settings.from_env({'SPEAK_HOME': str(tmp_path)}))
    assert not list(tmp_path.iterdir())


def test_manifest_download_is_pinned_verified_and_idempotent(monkeypatch, tmp_path):
    import hashlib
    payload = b'weights'
    manifest = {'files': {'model.safetensors': len(payload)},
                'sha256': {'model.safetensors': hashlib.sha256(payload).hexdigest()}}
    monkeypatch.setattr(synth, 'MANIFEST', manifest)
    monkeypatch.setattr(synth, 'require_mlx_platform', lambda: None)
    calls = []
    def download(repo, **kwargs):
        calls.append((repo, kwargs))
        (kwargs['local_dir'] / 'model.safetensors').write_bytes(payload)
    monkeypatch.setattr('huggingface_hub.snapshot_download', download)
    s = Settings.from_env({'SPEAK_HOME': str(tmp_path)})
    synth.ensure_models(s)
    assert synth.model_installed(s)
    assert calls[0][1]['revision'] == synth.MODEL_REVISION
    synth.ensure_models(s)
    assert len(calls) == 1
    (s.model_path / 'model.safetensors').write_bytes(b'bad')
    assert not synth.model_installed(s)


def test_checksum_failure_never_marks_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(synth, 'MANIFEST', {'files': {'model.safetensors': 3},
                                          'sha256': {'model.safetensors': 'invalid'}})
    monkeypatch.setattr(synth, 'require_mlx_platform', lambda: None)
    def download(repo, **kwargs):
        (kwargs['local_dir'] / 'model.safetensors').write_bytes(b'bad')
    monkeypatch.setattr('huggingface_hub.snapshot_download', download)
    s = Settings.from_env({'SPEAK_HOME': str(tmp_path)})
    with pytest.raises(ConfigError, match='checksum'):
        synth.ensure_models(s)
    assert not (s.model_path / '.speak-model.json').exists()


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    calls = []
    class Model:
        tokenizer = object()
        speech_tokenizer = object()
        def get_supported_speakers(self):
            return ['serena', 'ryan']
        def generate_custom_voice(self, **kwargs):
            calls.append(kwargs)
            print('model diagnostic')
            yield SimpleNamespace(audio=np.ones(20), sample_rate=24000)
            yield SimpleNamespace(audio=np.ones(10), sample_rate=24000)
    monkeypatch.setattr(synth, 'require_mlx_platform', lambda: None)
    monkeypatch.setattr(synth, 'model_installed', lambda s: True)
    monkeypatch.setattr(synth, '_load_model', lambda p: Model())
    s = Settings.from_env({'SPEAK_HOME': str(tmp_path)})
    return QwenSynth(s), calls


def test_generation_portuguese_and_protocol_clean(adapter, capsys):
    model, calls = adapter
    samples, sr = model.synthesize('Olá, Recife!')
    assert sr == 24000 and len(samples) == 30
    assert calls[0]['language'] == 'Portuguese'
    assert calls[0]['speaker'] == model.default_voice == 'serena'
    assert capsys.readouterr().out == ''


def test_invalid_voice_never_generates(adapter):
    model, calls = adapter
    with pytest.raises(ConfigError, match='unknown voice'):
        model.synthesize('Olá', 'pf_dora')
    assert calls == []


def test_empty_and_nonfinite_audio_fail(adapter, monkeypatch):
    model, _ = adapter
    for audio in [np.array([]), np.zeros(3), np.array([np.nan])]:
        monkeypatch.setattr(model._model, 'generate_custom_voice',
                            lambda **kw: iter([SimpleNamespace(audio=audio, sample_rate=24000)]))
        with pytest.raises(ConfigError, match='audio'):
            model.synthesize('Olá')


def test_encode_formats():
    tone = (0.2*np.sin(2*np.pi*440*np.arange(24000)/24000)).astype(np.float32)
    ogg = encode_ogg(tone, 24000)
    assert ogg[:4] == b'OggS' and b'OpusHead' in ogg[:200]
    wav = encode_wav(tone, 24000)
    assert wav[:4] == b'RIFF' and wav[8:12] == b'WAVE'


@pytest.mark.skipif(os.environ.get('SPEAK_TEST_REAL') != '1', reason='set SPEAK_TEST_REAL=1 for real Apple Silicon synthesis')
def test_real_synthesis_pt_br():
    model = QwenSynth(Settings.from_env())
    samples, sr = model.synthesize('Olá, este é um teste curto de áudio em português brasileiro.')
    assert sr == 24000 and 1 < len(samples)/sr < 30
    assert np.isfinite(samples).all() and np.max(np.abs(samples)) > 0.01
