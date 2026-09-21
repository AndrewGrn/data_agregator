from app.darknet.adapters.base import DarknetForumAdapter
from app.darknet.adapters.phpbb_like import PhpbbLikeAdapter
from app.darknet.adapters.xenforo_like import XenForoLikeAdapter


_ADAPTER_FACTORIES: dict[str, type[DarknetForumAdapter]] = {
    "xenforo": XenForoLikeAdapter,
    "phpbb_like": PhpbbLikeAdapter,
}

_ALIASES: dict[str, str] = {
    "xenforo": "xenforo",
    "xenforo_like": "xenforo",
    "xenforo-like": "xenforo",
    "phpbb": "phpbb_like",
    "phpbb_like": "phpbb_like",
    "phpbb-like": "phpbb_like",
}


def list_darknet_adapters() -> list[str]:
    return list(_ADAPTER_FACTORIES.keys())


def normalize_darknet_adapter(name: str | None) -> str:
    normalized = (name or "xenforo").strip().lower()
    if normalized in _ALIASES:
        return _ALIASES[normalized]
    raise ValueError(f"Unknown darknet adapter: {name}")


def suggest_adapter_for_detected(detected: str | None) -> str:
    normalized = (detected or "").strip().lower()
    if normalized in {"xenforo", "xenforo_like", "xenforo-like"}:
        return "xenforo"
    if normalized in {"phpbb", "phpbb_like", "phpbb-like"}:
        return "phpbb_like"
    if normalized in {"vbulletin", "vbulletin_like", "vbulletin-like"}:
        return "xenforo"
    return "xenforo"


def get_darknet_adapter(name: str | None) -> DarknetForumAdapter:
    canonical = normalize_darknet_adapter(name)
    adapter_type = _ADAPTER_FACTORIES.get(canonical)
    if not adapter_type:
        raise ValueError(f"Unknown darknet adapter: {name}")
    return adapter_type()
