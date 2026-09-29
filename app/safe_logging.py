"""Render exception diagnostics before logging; never attach raw exc_info/objects."""
import json
import os
import re
import traceback
from urllib.parse import quote, quote_plus

_SECRET_NAME = re.compile(r'token|secret|password|passwd|credential|api.?key|supabase_key|private.?key', re.I)


def redact(text: str, settings=None) -> str:
    values = [value for name, value in os.environ.items() if _SECRET_NAME.search(name) and value]
    if settings is not None:
        values.extend(value for name, value in vars(settings).items()
                      if _SECRET_NAME.search(name) and isinstance(value, str) and value)
    variants = set()
    for value in values:
        variants.update((value, quote(value, safe=''), quote_plus(value),
                         json.dumps(value)[1:-1], repr(value)[1:-1]))
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    # Also cover credentials in remote errors that are not our configured values.
    text = re.sub(r'\b\d+:[A-Za-z0-9_-]{20,}', '[REDACTED]', text)
    text = re.sub(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+', '[REDACTED]', text)
    text = re.sub(r'\bsb_secret_[\w-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s\'\"{},]+', '[REDACTED]', text)
    text = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(
        r'''(?ix)([\w-]*(?:token|secret|password|passwd|credential|api[_-]?key)[\w-]*["']?\s*[:=]\s*)
        (?:"[^"\n]*"|'[^'\n]*'|[^\s,;&}\]]+)''',
        r'\1[REDACTED]', text)
    return text


def log_exception(logger, event: str, error: BaseException, settings=None):
    # Include chained exceptions and frame locations, but never local variables,
    # request payloads, headers, or source lines (which may contain literals).
    diagnostic = traceback.TracebackException.from_exception(error, capture_locals=False)
    pending = [diagnostic]
    while pending:
        current = pending.pop()
        current.stack = traceback.StackSummary.from_list([
            traceback.FrameSummary(frame.filename, frame.lineno, frame.name,
                                   lookup_line=False, line='')
            for frame in current.stack
        ])
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
        pending.extend(getattr(current, 'exceptions', None) or [])
    text = ''.join(diagnostic.format())
    # PostgREST exposes useful database code/message/hint/details separately.
    for name in ('code', 'message', 'hint', 'details'):
        value = getattr(error, name, None)
        if isinstance(value, (str, int)):
            text += f'\n{name}: {value}'
    logger.error('%s\n%s', event, redact(text, settings))
