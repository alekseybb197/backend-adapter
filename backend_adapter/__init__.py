"""Backend adapter — domain packages.

Lazy proxies for module-level globals resolved at startup:
- SSL_CTX, _AVAILABLE_MODELS from config
- _d, _dr from logger
- _trace, _register_tool_use, _lookup_tool_use_producer from tracer
- SKILL_PATTERNS from skill
- _session_logs, _session_file_ts, _last_log_session_id from session_log
"""

# Lazy proxy — attributes resolved during init phase
_SSL_CTX = None
_AVAILABLE_MODELS: dict = {}


def _get_config_globals():
    """Called during startup to populate module-level globals."""
    global _SSL_CTX
    from .config import SSL_CTX, _AVAILABLE_MODELS
    _SSL_CTX = SSL_CTX
    _AVAILABLE_MODELS.update(_AVAILABLE_MODELS)
