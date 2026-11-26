"""Contract for content sources.

A provider must only return media from a documented, authorized interface: a public API with
license information, a file the user supplied, or a direct media URL the user supplied.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from ..db import DB


class SourceUnavailable(Exception):
    """The title can't be obtained; it should be hidden rather than retried."""


@dataclass
class Item:
    ref: str
    kind: str  # movie | episode
    title: str
    show: str | None = None
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    description: str = ""
    category: str | None = None  # None = decided during enrichment
    popularity: float = 0
    license: str | None = None
    source_url: str | None = None
    size: int | None = None
    md5: str | None = None
    runtime: float | None = None


@dataclass
class Source:
    url: str
    size: int | None = None
    md5: str | None = None
    runtime: float | None = None
    description: str = ""
    poster_url: str | None = None
    height: int | None = None
    title: str | None = None  # better title than the one found during discovery
    extra: list[Item] = field(default_factory=list)  # further titles found inside the same entry


class Provider(ABC):
    name: str
    label: str
    interval_hours: float = 24
    remote = True  # False: source_url is a local path

    def enabled(self, db: DB) -> bool:
        return True

    @abstractmethod
    def discover(self, db: DB) -> Iterable[Item]: ...

    @abstractmethod
    def resolve(self, media: dict) -> Source:
        """Return the concrete media file for a title, or raise SourceUnavailable."""
