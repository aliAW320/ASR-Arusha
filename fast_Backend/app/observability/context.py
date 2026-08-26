from contextvars import ContextVar, Token
from typing import Any


_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})


def start_log_context(**fields: Any) -> Token:
    return _log_context.set({key: value for key, value in fields.items() if value is not None})


def bind_log_context(**fields: Any) -> None:
    context = dict(_log_context.get())
    for key, value in fields.items():
        if value is None:
            context.pop(key, None)
        else:
            context[key] = value
    _log_context.set(context)


def get_log_context() -> dict[str, Any]:
    return dict(_log_context.get())


def reset_log_context(token: Token) -> None:
    _log_context.reset(token)
