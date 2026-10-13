# Speak: Qwen3-TTS 1.7B via MLX 4-bit

As of 2026-10-13, replace Kokoro with `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit`, pinned to revision `f35faf19b0cc2160865af64ecf0f22f83d335135`. Preserve the three tool schemas, sink selection, queue semantics, validation, and credential boundaries. See [speak reference](../../speak/README.md).

Portuguese is explicit and independent of voice. Preset timbre defaults to `serena`; style requests natural, clear Brazilian Portuguese delivery. Presets are multilingual, not native Brazilian recordings; audible quality must be judged separately. `SPEAK_SPEED` becomes an approximate instruction because this backend has no exact speed parameter.

Only this model snapshot is downloaded, including its own text tokenizer and speech decoder. A file manifest records sizes and weight hashes. Readiness is written only after verification; loading checks marker and sizes. Lazy imports preserve clean MCP startup without MLX on unsupported platforms. Model Python diagnostics go to stderr. Generation is locked because local sinks pipeline work across threads.

MLX requires macOS Apple Silicon. Retire the Linux cloud installer without altering existing registrations or packages: the setup script logs and exits successfully. Portable CI checks fake synthesis contracts and protocol startup on Linux/macOS; real model tests require an explicit local opt-in and do not claim creative quality. Existing Linux/Oracle deployments remain on their previously installed release and need a separate migration if desired.

Local activation can use a dedicated Python environment and a stable launcher while the repository change awaits review; a cached plugin override must be documented because a future marketplace update may replace it. Do not merge merely to activate locally.
