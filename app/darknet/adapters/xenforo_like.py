from __future__ import annotations

from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.darknet.adapters.base import DarknetForumAdapter, ParsedThreadPost, ParsedThreadResult


def _text(value) -> str:
    return value.get_text(" ", strip=True) if value else ""


class XenForoLikeAdapter(DarknetForumAdapter):
    name = "xenforo_like"

    async def _fetch_html(self, client, url: str) -> str:
        async with client.get(url, allow_redirects=True) as response:
            return await response.text(errors="ignore")

    def _extract_xf_token(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        token_input = soup.select_one("input[name=_xfToken]")
        if token_input and token_input.get("value"):
            return str(token_input["value"])
        return None

    async def login(self, client, base_url: str, account_credentials: dict) -> bool:
        username = (account_credentials or {}).get("username", "").strip()
        password = (account_credentials or {}).get("password", "").strip()
        if not username or not password:
            return False

        login_page_path = (account_credentials or {}).get("login_page_path", "/login/")
        login_submit_path = (account_credentials or {}).get("login_submit_path", "/login/login")

        login_page_url = urljoin(base_url, login_page_path)
        login_submit_url = urljoin(base_url, login_submit_path)

        html = await self._fetch_html(client, login_page_url)
        token = self._extract_xf_token(html) or ""

        payload = {
            "login": username,
            "password": password,
            "remember": "1",
            "_xfRedirect": base_url,
        }
        if token:
            payload["_xfToken"] = token

        headers = {"Referer": login_page_url}
        async with client.post(login_submit_url, data=payload, headers=headers, allow_redirects=True) as response:
            body = await response.text(errors="ignore")
            body_lower = body.lower()
            if response.status >= 400:
                return False
            if "logout" in body_lower:
                return True
            # Fallback heuristic: many forums redirect to homepage after successful login
            if "login" not in str(response.url).lower():
                return True
            return False

    def _is_same_host(self, base_host: str, candidate_url: str) -> bool:
        host = urlparse(candidate_url).netloc
        return host == base_host

    def _extract_thread_links(self, html: str, page_url: str, thread_url_contains: str, base_host: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for node in soup.select("a[href]"):
            href = node.get("href")
            if not href:
                continue
            absolute = urljoin(page_url, href)
            if thread_url_contains not in absolute:
                continue
            if not self._is_same_host(base_host, absolute):
                continue
            if absolute not in links:
                links.append(absolute)
        return links

    async def discover_thread_urls(self, client, start_urls: list[str], config: dict) -> list[str]:
        thread_url_contains = str(config.get("thread_url_contains", "/threads/")).strip() or "/threads/"
        links: list[str] = []
        if not start_urls:
            return links

        base_host = urlparse(start_urls[0]).netloc

        for url in start_urls:
            html = await self._fetch_html(client, url)
            for thread_url in self._extract_thread_links(html, url, thread_url_contains, base_host):
                if thread_url not in links:
                    links.append(thread_url)
        return links

    def _find_next_page(self, html: str, current_url: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("a.pageNav-jump--next, a.pageNav-next, a[rel=next]")
        if node and node.get("href"):
            return urljoin(current_url, node.get("href"))
        return None

    def _parse_posts_from_html(self, html: str) -> tuple[str | None, list[ParsedThreadPost], list[dict]]:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("h1.p-title-value, h1.title, h1")
        thread_title = _text(title_node) or None

        posts: list[ParsedThreadPost] = []
        users_map: dict[str, dict] = {}

        post_nodes: Iterable = soup.select("article.message, li.message, div.message")
        for index, node in enumerate(post_nodes):
            post_id = str(node.get("data-content") or node.get("id") or f"idx-{index}")
            author_node = node.select_one(".username, .message-name a, .userText a")
            author = _text(author_node) or None
            time_node = node.select_one("time[datetime], .u-dt")
            posted_at = None
            if time_node:
                posted_at = time_node.get("datetime") or _text(time_node) or None
            body_node = node.select_one(".message-body, .bbWrapper, .content")
            text = _text(body_node)
            raw = {
                "post_id": post_id,
                "author": author,
                "posted_at": posted_at,
            }
            posts.append(ParsedThreadPost(post_id=post_id, author=author, posted_at=posted_at, text=text, raw=raw))

            if author and author not in users_map:
                users_map[author] = {"username": author}

        users = list(users_map.values())
        return thread_title, posts, users

    async def parse_thread(self, client, thread_url: str, config: dict) -> ParsedThreadResult:
        max_pages = max(int(config.get("max_pages_per_thread", 1)), 1)
        max_posts = max(int(config.get("max_posts_per_thread", 500)), 1)

        page_url = thread_url
        visited: set[str] = set()
        all_posts: list[ParsedThreadPost] = []
        all_users: dict[str, dict] = {}
        thread_title: str | None = None

        while page_url and len(visited) < max_pages:
            if page_url in visited:
                break
            visited.add(page_url)

            html = await self._fetch_html(client, page_url)
            title, posts, users = self._parse_posts_from_html(html)
            if title and not thread_title:
                thread_title = title

            for post in posts:
                if len(all_posts) >= max_posts:
                    break
                all_posts.append(post)
            for user in users:
                key = user.get("username") or str(user)
                if key not in all_users:
                    all_users[key] = user

            if len(all_posts) >= max_posts:
                break
            page_url = self._find_next_page(html, page_url)

        return ParsedThreadResult(
            thread_url=thread_url,
            thread_title=thread_title,
            posts=all_posts,
            users=list(all_users.values()),
        )
