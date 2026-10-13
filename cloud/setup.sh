#!/bin/bash
# Qwen3-TTS via MLX is macOS Apple Silicon only. Do not overwrite a working
# Linux deployment with a package that cannot synthesize there.
# Preserve the setup-script contract: bounded, explanatory, always exit 0.
echo 'speak-setup: Qwen3-TTS MLX requires macOS Apple Silicon; Linux cloud provisioning is retired.' >&2
echo 'speak-setup: no packages, models, credentials, or MCP registrations were changed.' >&2
exit 0
