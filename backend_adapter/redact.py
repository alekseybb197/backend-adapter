"""Secret masking for logs and trace.

Policies that prevent secrets from being written to debug/trace logs:
- Authorization: Bearer <token>
- Variables like *_PAT / *_KEY / *_TOKEN / *_SECRET = <value>
- Long base64/hex strings that may be tokens
"""
import re


# ==================== REDACTION ====================
_SECRET_PATTERNS = [
    (re.compile(r'(Bearer\s+)([A-Za-z0-9\-_\.=/+]{6,})', re.IGNORECASE),
     lambda m: m.group(1) + _mask(m.group(2))),
    (re.compile(r'((?:[A-Z0-9_]*(?:_PAT|_KEY|_TOKEN|_SECRET|API_KEY)[A-Z0-9_]*)\s*[:=]\s*["\']?)([A-Za-z0-9\-_\.\/+=]{8,})',
                re.IGNORECASE),
     lambda m: m.group(1) + _mask(m.group(2))),
]


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***REDACTED***"
    return f"{value[:4]}***REDACTED***{value[-4:]}"


def redact(text: str) -> str:
    """Применяет все паттерны редактирования секретов к произвольной строке."""
    if not text:
        return text
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            out[k] = redact(v)
        else:
            out[k] = v
    return out
