_SENTINEL = object()


def __getattr__(name: str):
    import importlib

    _mod = importlib.import_module(f"{__name__}.agent")
    val = getattr(_mod, name, _SENTINEL)
    if val is not _SENTINEL:
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
