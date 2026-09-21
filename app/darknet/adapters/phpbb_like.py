from __future__ import annotations

from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from app.darknet.adapters.base import DarknetForumAdapter, ParsedThreadPost, ParsedThreadResult


def _text(value) -> str:
    return value.get_text(" ", strip=True) if value else ""


class PhpbbLikeAdapter(DarknetForumAdapter):
    name = "phpbb_like"

    @staticmethod
    def _limit_value(config: dict, key: str, default: int) -> int:
        try:
            return int(config.get(key, default))
        except Exception:
            return int(default)

    async def _fetch_html(self, client, url: str) -> str:
        async with client.get(url, allow_redirects=True) as response:
            return await response.text(errors="ignore")

    def _extract_hidden_inputs(self, html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        hidden: dict[str, str] = {}
        for node in soup.select("input[type=hidden][name]"):
            name = str(node.get("name") or "").strip()
            if not name:
                continue
            hidden[name] = str(node.get("value") or "")
        return hidden

    async def _is_authenticated(self, client, base_url: str, account_credentials: dict) -> bool:
        check_path = str((account_credentials or {}).get("auth_check_path") or "/").strip() or "/"
        check_url = urljoin(base_url, check_path)
        html = await self._fetch_html(client, check_url)
        if not html:
            return False
        lower = html.lower()
        if "mode=logout" in lower or "logout" in lower:
            return True
        return "mode=login" not in lower and "ucp.php?mode=login" not in lower

    async def login(self, client, base_url: str, account_credentials: dict) -> bool:
        auth_mode = str((account_credentials or {}).get("auth_mode") or "").strip().lower()
        if auth_mode in {"browser_state", "storage_state", "cookies"}:
            authenticated = await self._is_authenticated(client, base_url, account_credentials or {})
            if authenticated:
                return True
            fallback_form_login = bool((account_credentials or {}).get("fallback_form_login", False))
            if not fallback_form_login:
                return False

        username = (account_credentials or {}).get("username", "").strip()
        password = (account_credentials or {}).get("password", "").strip()
        if not username or not password:
            return False

        login_page_path = (account_credentials or {}).get("login_page_path", "/ucp.php?mode=login")
        login_submit_path = (account_credentials or {}).get("login_submit_path", "/ucp.php?mode=login")
        login_page_url = urljoin(base_url, login_page_path)
        login_submit_url = urljoin(base_url, login_submit_path)

        html = await self._fetch_html(client, login_page_url)
        payload = self._extract_hidden_inputs(html)
        payload.update(
            {
                "username": username,
                "password": password,
                "login": payload.get("login", "Login"),
                "autologin": payload.get("autologin", "on"),
            }
        )

        headers = {"Referer": login_page_url}
        async with client.post(login_submit_url, data=payload, headers=headers, allow_redirects=True) as response:
            body = await response.text(errors="ignore")
            body_lower = body.lower()
            if response.status >= 400:
                return False
            if "logout" in body_lower or "mode=logout" in body_lower:
                return True
            if "ucp.php?mode=login" not in str(response.url).lower():
                return True
            return False

    def _is_same_host(self, base_host: str, candidate_url: str) -> bool:
        host = urlparse(candidate_url).netloc
        return host == base_host

    def _normalize_url(self, url: str) -> str:
        normalized, _ = urldefrag(url)
        return normalized

    def _extract_thread_links(self, html: str, page_url: str, thread_url_contains: str, base_host: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for node in soup.select("a[href]"):
            href = node.get("href")
            if not href:
                continue
            absolute = self._normalize_url(urljoin(page_url, href))
            if thread_url_contains not in absolute:
                continue
            if not self._is_same_host(base_host, absolute):
                continue
            if absolute not in links:
                links.append(absolute)
        return links

    async def discover_thread_urls(self, client, start_urls: list[str], config: dict) -> list[str]:
        thread_url_contains = str(config.get("thread_url_contains", "/viewtopic.php")).strip() or "/viewtopic.php"
        collect_maximum = bool(config.get("collect_maximum", True))
        if collect_maximum:
            max_discover_pages_per_start = 5000
        else:
            raw = self._limit_value(config, "max_discover_pages_per_start", 20)
            max_discover_pages_per_start = 5000 if raw <= 0 else min(max(raw, 1), 5000)

        links: list[str] = []
        if not start_urls:
            return links

        base_host = urlparse(start_urls[0]).netloc
        for url in start_urls:
            page_url = url
            visited_pages: set[str] = set()
            while page_url and len(visited_pages) < max_discover_pages_per_start:
                if page_url in visited_pages:
                    break
                visited_pages.add(page_url)

                html = await self._fetch_html(client, page_url)
                for thread_url in self._extract_thread_links(html, page_url, thread_url_contains, base_host):
                    if thread_url not in links:
                        links.append(thread_url)

                next_url = self._find_next_page(html, page_url)
                if next_url and not self._is_same_host(base_host, next_url):
                    break
                page_url = next_url
        return links

    def _find_next_page(self, html: str, current_url: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        node = (
            soup.select_one("a[rel=next]")
            or soup.select_one(".pagination .next a")
            or soup.select_one(".pagination li.next a")
        )
        if not node:
            for candidate in soup.select("a[href]"):
                title = str(candidate.get("title") or "").lower()
                css = " ".join(candidate.get("class") or []).lower()
                text = _text(candidate).lower()
                if "next" in title or "next" in css or text in {"next", ">", ">>", "далі", "далее"}:
                    node = candidate
                    break
        if node and node.get("href"):
            return self._normalize_url(urljoin(current_url, node.get("href")))
        return None

    def _parse_posts_from_html(self, html: str) -> tuple[str | None, list[ParsedThreadPost], list[dict]]:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("h2.topic-title a, h2.topic-title, .topic-title a, #page-body h2")
        thread_title = _text(title_node) or None

        posts: list[ParsedThreadPost] = []
        users_map: dict[str, dict] = {}

        post_nodes: Iterable = soup.select("div.post, article.post, li.post")
        for index, node in enumerate(post_nodes):
            post_id = str(node.get("id") or node.get("data-post-id") or f"idx-{index}")
            author_node = node.select_one("a.username, a.username-coloured, .author a, .postprofile a.username")
            author = _text(author_node) or None
            time_node = node.select_one("time[datetime], .author, p.author")
            posted_at = None
            if time_node:
                posted_at = time_node.get("datetime") or _text(time_node) or None
            body_node = node.select_one("div.content, .postbody .content, .postbody")
            text = _text(body_node)
            if not text:
                continue

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
        collect_maximum = bool(config.get("collect_maximum", True))
        if collect_maximum:
            max_pages = 100000
            max_posts = 1000000
        else:
            raw_pages = self._limit_value(config, "max_pages_per_thread", 1)
            raw_posts = self._limit_value(config, "max_posts_per_thread", 500)
            max_pages = 100000 if raw_pages <= 0 else min(max(raw_pages, 1), 100000)
            max_posts = 1000000 if raw_posts <= 0 else min(max(raw_posts, 1), 1000000)

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
