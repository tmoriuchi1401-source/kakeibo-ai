"""Recognize both supported GenAI error families without trusting error bodies."""
from functools import lru_cache
from importlib import import_module

from google.genai.errors import APIError


@lru_cache(maxsize=1)
def _api_error_types():
    types = [APIError]
    # Interactions has a separate hierarchy, including in google-genai 2.24.
    # Earlier Interactions releases used the other compatibility path.
    for module in ('google.genai._gaos.lib.compat_errors', 'google.genai._interactions'):
        try:
            candidate = getattr(import_module(module), 'APIError', None)
        except ModuleNotFoundError as error:
            if error.name != module and not module.startswith(error.name + '.'):
                raise
            continue
        if isinstance(candidate, type) and issubclass(candidate, Exception):
            types.append(candidate)
    return tuple(types)


def is_gemini_api_error(error):
    return isinstance(error, _api_error_types())


def gemini_api_status(error):
    """Only actual SDK exceptions may qualify for extraction deferral."""
    if not is_gemini_api_error(error):
        return None
    code = getattr(error, 'status_code', None)
    if code is None:
        code = getattr(error, 'code', None)
    return code if type(code) is int and 400 <= code < 600 else None
