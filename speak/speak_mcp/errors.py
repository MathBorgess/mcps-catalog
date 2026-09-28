class ConfigError(RuntimeError):
    """The environment is missing something the server needs (env var, model file, espeak-ng)."""


class SendError(RuntimeError):
    """Telegram rejected or never received a request. Message never carries the token."""
