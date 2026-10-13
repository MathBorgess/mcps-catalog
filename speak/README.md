# speak

Text-to-speech MCP using [Qwen3-TTS 1.7B CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice), converted to [MLX 4-bit](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit). Runs on **macOS Apple Silicon**. Portuguese is the default language. Local playback uses `afplay`; the same Mac can send Telegram voice messages using its configured sink. No Kokoro, ONNX, espeak-ng, or alternate synthesis backend.

## Install

Install `uv`, then install the plugin using the [repository instructions](../README.md). The launcher uses `uvx --from ${CLAUDE_PLUGIN_ROOT} speak-mcp`. For manual setup from this checkout:

```bash
uvx --from ./speak speak-mcp --setup
```

The single pinned snapshot occupies approximately 2.31 GB, including its bundled speech decoder. Generation needs additional memory; the 4-bit weights do not imply a fixed RAM limit or measured real-time performance. Linux and Intel Macs return `ok: false` with an explicit platform error. The previous Linux cloud setup is retired; a Linux VM cannot run MLX. Existing deployments are not upgraded by editing this repository.

## Tools

| Tool | Arguments | Behavior |
|---|---|---|
| `speak` | `text` (1–12000 chars), `title?`, `voice?` | Queue local playback, or send one Telegram voice message. |
| `speak_clips` | `clips: [{text, caption?}]` (1–20), `note?`, `voice?` | Play clips in sequence; Telegram sends one voice message per clip. |
| `speak_stop` | — | Stop local playback and clear pending jobs; Telegram is a no-op. |

The API shape is unchanged. **Voice selects timbre, not language.** Available speaker IDs are `serena`, `vivian`, `uncle_fu`, `ryan`, `aiden`, `ono_anna`, `sohee`, `eric`, `dylan`. Old Kokoro IDs such as `pf_dora` are rejected; remove those overrides. The preset speakers are not native Brazilian voices; Portuguese quality and accent should be judged from samples, not parameter count.

Text is chunked into approximately 400-character sentence pieces to bound generation memory. Synthesis is serialized across threads. Local results report queue acceptance, not playback success; worker failures go to stderr. Telegram waits up to `SPEAK_WAIT_SECONDS` then reports `queued: true` with pending clips. **Do not resend accepted clips**, because delivery continues in the background. Normal shutdown waits up to 5 minutes for Telegram; SIGTERM exits promptly. Telegram credentials and delivery are unchanged, and no tools accept destinations or file paths. Titles/captions/notes reject control characters and `://`/`www.`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SPEAK_HOME` | `~/.cache/speak-mcp` | Pinned model lives under `models/qwen3-tts-1.7b-customvoice-4bit`. |
| `SPEAK_VOICE` | `serena` | Qwen speaker ID. |
| `SPEAK_LANGUAGE` | `Portuguese` | Portuguese, English, Chinese, Japanese, Korean, German, French, Russian, Spanish, Italian, or Auto. |
| `SPEAK_INSTRUCT` | Natural, clear Brazilian Portuguese delivery | Style instruction; language-specific neutral default for other languages. |
| `SPEAK_SPEED` | `1.0` | `0.7`–`1.5`, expressed as an approximate pace instruction. Qwen CustomVoice has no exact numerical speed control. |
| `SPEAK_SINK` | Auto | `local` or `telegram`; auto uses Telegram for remote-session flags. |
| `SPEAK_WAIT_SECONDS` | `45` | Telegram wait before replying queued, `0`–`3600`. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Required only for Telegram. |

## Model and integrity

`model_manifest.json` pins the snapshot revision, file sizes, and SHA-256 hashes of both weight files. Hugging Face downloads resumable temporary files then renames them. Setup verifies sizes and hashes before writing the installation marker. Subsequent loads check the marker and sizes; no unpinned model or fallback weights are fetched. MLX and tokenizers load lazily; model diagnostics go to stderr to preserve the MCP stdout protocol. `--setup` downloads and loads; it does not generate speech. Test synthesis separately.

## CLI and validation

```bash
speak-mcp --setup
speak-mcp --sample "Olá, este é um teste em português brasileiro." --out /tmp/teste.ogg
speak-mcp --say "Olá"  # plays locally
cd speak
python -m pytest -q -rs
SPEAK_TEST_REAL=1 python -m pytest -q -rs tests/test_synth.py::test_real_synthesis_pt_br
```

Portable CI exercises the adapter with fake model output and checks the real stdio handshake. `SPEAK_TEST_REAL=1` explicitly enables real synthesis on an Apple Silicon host with installed weights. Tests do not send real Telegram messages. Real synthesis validates non-empty finite audio, not pronunciation or creative quality.
