from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedThreadPost:
    post_id: str
    author: str | None
    posted_at: str | None
    text: str
    raw: dict


@dataclass(slots=True)
class ParsedThreadResult:
    thread_url: str
    thread_title: str | None
    posts: list[ParsedThreadPost]
    users: list[dict]


class DarknetForumAdapter(ABC):
    name: str

    @abstractmethod
    async def login(self, client, base_url: str, account_credentials: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def discover_thread_urls(self, client, start_urls: list[str], config: dict) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def parse_thread(self, client, thread_url: str, config: dict) -> ParsedThreadResult:
        raise NotImplementedError
