"""Adapter registry: name -> factory, used by the CLI and the crawler."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from jobscout.adapters.base import Adapter
from jobscout.adapters.fixture import FixtureAdapter, FixtureBundle, load_fixture_adapter
from jobscout.adapters.hnhiring import HnHiringAdapter
from jobscout.adapters.htmlboard import HtmlBoardAdapter
from jobscout.errors import ConfigError
from jobscout.parse.board import BoardConfig

AdapterFactory = Callable[..., Adapter]

#: Bundled selector configs, by board name (shipped inside the package).
SELECTORS_DIR = Path(__file__).resolve().parent.parent / "selectors"

_FACTORIES: dict[str, AdapterFactory] = {
    "hnhiring": lambda **kwargs: HnHiringAdapter(**kwargs),
    "htmlboard": lambda **kwargs: HtmlBoardAdapter(
        BoardConfig.from_yaml(kwargs.pop("config_path")), **kwargs
    ),
    "fixture": lambda **kwargs: load_fixture_adapter(kwargs.pop("path")),
}


def available_adapters() -> list[str]:
    return sorted(_FACTORIES)


def get_adapter(name: str, **kwargs: object) -> Adapter:
    """Build an adapter by name.

    ``htmlboard`` needs ``config_path`` and ``fixture`` needs ``path``; all
    others take their own keyword arguments.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ConfigError(f"unknown adapter {name!r}; available: {', '.join(available_adapters())}")
    return factory(**kwargs)


def board_config_path(board: str) -> Path:
    """Resolve a bundled selector config, e.g. ``htmlboard`` -> ``demo_board.yml``."""
    candidate = SELECTORS_DIR / f"{board}.yml"
    if not candidate.is_file():
        available = sorted(p.stem for p in SELECTORS_DIR.glob("*.yml"))
        raise ConfigError(f"no selector config for {board!r}; available: {', '.join(available)}")
    return candidate


__all__ = [
    "SELECTORS_DIR",
    "Adapter",
    "FixtureAdapter",
    "FixtureBundle",
    "HnHiringAdapter",
    "HtmlBoardAdapter",
    "available_adapters",
    "board_config_path",
    "get_adapter",
    "load_fixture_adapter",
]
