from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings
from app.models import RawEvent, Target

try:
    from opensearchpy import OpenSearch
except Exception:  # pragma: no cover - optional dependency in some dev runs
    OpenSearch = None  # type: ignore[assignment]

settings = get_settings()


def _coerce_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _parse_iso_datetime(value: str | None) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    return _coerce_utc(parsed)


class MessageSearchIndex:
    def __init__(self) -> None:
        self._client = None
        self._index_ready = False

    @property
    def index_name(self) -> str:
        raw = str(settings.opensearch_index_messages or "").strip()
        return raw or "messages-v1"

    def _enabled(self) -> bool:
        return bool(settings.opensearch_enabled and str(settings.opensearch_url or "").strip())

    def _get_client(self):
        if not self._enabled() or OpenSearch is None:
            return None
        if self._client is not None:
            return self._client

        parsed = urlparse(str(settings.opensearch_url))
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "opensearch"
        port = int(parsed.port or (443 if scheme == "https" else 9200))

        username = str(settings.opensearch_username or "").strip()
        password = str(settings.opensearch_password or "")
        http_auth = (username, password) if username else None

        self._client = OpenSearch(
            hosts=[{"host": host, "port": port, "scheme": scheme}],
            http_auth=http_auth,
            verify_certs=bool(settings.opensearch_verify_certs),
            ssl_assert_hostname=False,
            ssl_show_warn=False,
            timeout=max(int(settings.opensearch_timeout_seconds), 1),
        )
        return self._client

    def _ensure_index(self) -> bool:
        if self._index_ready:
            return True
        client = self._get_client()
        if client is None:
            return False
        try:
            exists = bool(client.indices.exists(index=self.index_name))
            if not exists:
                client.indices.create(index=self.index_name, body=self._index_body())
            self._index_ready = True
            return True
        except Exception as exc:
            print(f"[search-index] ensure index failed: {exc}", flush=True)
            message = str(exc).lower()
            if "create-index blocked" in message or "cluster create-index blocked" in message:
                self._try_clear_cluster_create_block(client)
                try:
                    exists = bool(client.indices.exists(index=self.index_name))
                    if not exists:
                        client.indices.create(index=self.index_name, body=self._index_body())
                    self._index_ready = True
                    return True
                except Exception as retry_exc:
                    print(f"[search-index] ensure index retry failed: {retry_exc}", flush=True)
            return False

    def _try_clear_cluster_create_block(self, client) -> None:
        try:
            client.cluster.put_settings(body={"persistent": {"cluster.blocks.create_index": False}})
        except Exception as exc:
            print(f"[search-index] cannot clear create_index block: {exc}", flush=True)

    def _try_clear_read_only_block(self, client) -> None:
        try:
            client.cluster.put_settings(body={"persistent": {"cluster.routing.allocation.disk.threshold_enabled": False}})
        except Exception as exc:
            print(f"[search-index] cannot disable disk threshold: {exc}", flush=True)
        try:
            client.indices.put_settings(index=self.index_name, body={"index.blocks.read_only_allow_delete": False})
        except Exception as exc:
            print(f"[search-index] cannot clear read_only block on {self.index_name}: {exc}", flush=True)

    def _index_body(self) -> dict[str, Any]:
        return {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "event_id": {"type": "long"},
                    "parser_type": {"type": "keyword"},
                    "target_id": {"type": "long"},
                    "target_name": {"type": "text"},
                    "target_identifier": {"type": "keyword"},
                    "account_id": {"type": "long"},
                    "owner_user_id": {"type": "long"},
                    "external_id": {"type": "keyword"},
                    "event_type": {"type": "keyword"},
                    "is_comment": {"type": "boolean"},
                    "sender_id": {"type": "long"},
                    "sender_username": {"type": "keyword"},
                    "sender_label": {"type": "text"},
                    "text": {"type": "text"},
                    "observed_at": {"type": "date"},
                    "created_at": {"type": "date"},
                    "root_post_id": {"type": "long"},
                    "parent_message_id": {"type": "long"},
                }
            },
        }

    def _doc_id(self, event: RawEvent) -> str:
        parser_type = getattr(event.parser_type, "value", str(event.parser_type))
        external_id = str(event.external_id or "").strip()
        if external_id:
            return f"{parser_type}:{int(event.target_id)}:{external_id}"
        return f"{parser_type}:event:{int(event.id)}"

    def _payload_from_preview(self, event: RawEvent) -> dict[str, Any] | None:
        preview = event.payload_preview if isinstance(event.payload_preview, dict) else {}
        if not preview:
            return None

        parser_type = getattr(event.parser_type, "value", str(event.parser_type))
        if parser_type == "telegram":
            raw_event_type = str(preview.get("event_type") or "").strip().lower()
            message_id = preview.get("message_id")
            text_value = str(preview.get("text") or "").strip()
            if raw_event_type in {"telegram_message", "telegram_comment"}:
                event_type = raw_event_type
            elif message_id is not None or text_value:
                event_type = "telegram_message"
            else:
                return None
            payload: dict[str, Any] = {"event_type": event_type}
            if message_id is not None:
                try:
                    payload["message_id"] = int(message_id)
                except Exception:
                    payload["message_id"] = message_id
            date_raw = preview.get("date")
            if date_raw:
                payload["date"] = date_raw
            if text_value:
                payload["text"] = text_value
            return payload

        if parser_type == "darknet":
            text_value = str(preview.get("text") or "").strip()
            title = str(preview.get("title") or "").strip()
            if not text_value and not title:
                return None
            payload = {"event_type": str(preview.get("event_type") or "darknet_event")}
            if title:
                payload["title"] = title
            if text_value:
                payload["text"] = text_value
            url = preview.get("url")
            if url:
                payload["url"] = url
            return payload

        return None

    def _normalize_payload_for_index(self, event: RawEvent, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if isinstance(payload, dict) and payload:
            parser_type = getattr(event.parser_type, "value", str(event.parser_type))
            raw_event_type = str(payload.get("event_type") or "").strip().lower()
            if parser_type == "telegram":
                if raw_event_type in {"telegram_message", "telegram_comment"}:
                    return payload
                if payload.get("message_id") is not None or str(payload.get("text") or "").strip():
                    synthesized = dict(payload)
                    synthesized["event_type"] = "telegram_message"
                    return synthesized
            elif parser_type == "darknet":
                if str(payload.get("text") or "").strip() or str(payload.get("title") or "").strip() or str(payload.get("content") or "").strip():
                    if raw_event_type:
                        return payload
                    synthesized = dict(payload)
                    synthesized["event_type"] = "darknet_event"
                    return synthesized

        return self._payload_from_preview(event)

    def _build_doc(self, event: RawEvent, payload: dict[str, Any] | None, target: Target | None) -> dict[str, Any] | None:
        parser_type = getattr(event.parser_type, "value", str(event.parser_type))
        data = payload if isinstance(payload, dict) else {}

        sender_id = None
        sender_username = None
        sender_label = None
        text_value = ""
        event_type = str(data.get("event_type") or "")
        is_comment = False
        root_post_id = None
        parent_message_id = None

        observed_at = _coerce_utc(event.observed_at) or _coerce_utc(event.created_at) or dt.datetime.now(dt.UTC)
        payload_date = _parse_iso_datetime(data.get("date"))
        if payload_date:
            observed_at = payload_date

        if parser_type == "telegram":
            if event_type not in {"telegram_message", "telegram_comment"}:
                return None
            sender_raw = data.get("sender")
            sender = sender_raw if isinstance(sender_raw, dict) else {}
            sender_id_raw = sender.get("id")
            try:
                sender_id = int(sender_id_raw) if sender_id_raw is not None else None
            except Exception:
                sender_id = None
            sender_username_raw = str(sender.get("username") or "").strip().removeprefix("@").lower()
            sender_username = sender_username_raw or None
            first_name = str(sender.get("first_name") or "").strip()
            last_name = str(sender.get("last_name") or "").strip()
            full_name = " ".join(part for part in [first_name, last_name] if part).strip()
            sender_label = f"@{sender_username}" if sender_username else (full_name or (f"ID {sender_id}" if sender_id else "-"))
            text_value = str(data.get("text") or "").strip()
            is_comment = event_type == "telegram_comment"
            root_post_id = data.get("root_post_id")
            parent_message_id = data.get("parent_message_id")
        elif parser_type == "darknet":
            event_type = event_type or "darknet_event"
            author = str(data.get("author") or "").strip()
            sender_label = author or None
            sender_username = author.lower() if author else None
            text_chunks = [
                str(data.get("title") or "").strip(),
                str(data.get("text") or "").strip(),
                str(data.get("content") or "").strip(),
            ]
            text_value = "\n".join([chunk for chunk in text_chunks if chunk])
            if not text_value:
                return None
        else:
            return None

        return {
            "event_id": int(event.id),
            "parser_type": parser_type,
            "target_id": int(event.target_id),
            "target_name": str(target.name) if target else None,
            "target_identifier": str(target.identifier) if target else None,
            "account_id": int(event.account_id) if event.account_id is not None else None,
            "owner_user_id": int(event.owner_user_id) if event.owner_user_id is not None else None,
            "external_id": str(event.external_id) if event.external_id else None,
            "event_type": event_type,
            "is_comment": bool(is_comment),
            "sender_id": sender_id,
            "sender_username": sender_username,
            "sender_label": sender_label,
            "text": text_value,
            "observed_at": observed_at.isoformat(),
            "created_at": _coerce_utc(event.created_at).isoformat() if event.created_at else None,
            "root_post_id": int(root_post_id) if isinstance(root_post_id, int) else None,
            "parent_message_id": int(parent_message_id) if isinstance(parent_message_id, int) else None,
        }

    def index_raw_event(self, event: RawEvent, payload: dict[str, Any] | None, target: Target | None = None) -> bool:
        if not self._ensure_index():
            return False
        client = self._get_client()
        if client is None:
            return False
        normalized_payload = self._normalize_payload_for_index(event=event, payload=payload)
        document = self._build_doc(event=event, payload=normalized_payload, target=target)
        if not document:
            return False
        try:
            client.index(index=self.index_name, id=self._doc_id(event), body=document, refresh=False)
            return True
        except Exception as exc:
            print(f"[search-index] index event #{event.id} failed: {exc}", flush=True)
            message = str(exc).lower()
            if "read-only-allow-delete" in message or "flood-stage watermark" in message:
                self._try_clear_read_only_block(client)
                try:
                    client.index(index=self.index_name, id=self._doc_id(event), body=document, refresh=False)
                    return True
                except Exception as retry_exc:
                    print(f"[search-index] index retry event #{event.id} failed: {retry_exc}", flush=True)
            return False

    def search_messages(
        self,
        query: str,
        limit: int,
        owner_user_id: int | None,
        parser_type: str | None = None,
        target_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self._ensure_index():
            raise RuntimeError("OpenSearch недоступний або вимкнений")
        client = self._get_client()
        if client is None:
            raise RuntimeError("OpenSearch недоступний або вимкнений")

        safe_limit = max(10, min(int(limit or 100), 200))
        text_query = str(query or "").strip()

        filters: list[dict[str, Any]] = []
        if owner_user_id is not None:
            filters.append({"term": {"owner_user_id": int(owner_user_id)}})
        if parser_type:
            filters.append({"term": {"parser_type": str(parser_type)}})
        if target_id is not None:
            filters.append({"term": {"target_id": int(target_id)}})

        if text_query:
            must_query: list[dict[str, Any]] = [
                {
                    "simple_query_string": {
                        "query": text_query,
                        "fields": [
                            "text^4",
                            "sender_label^2",
                            "sender_username^2",
                            "target_name^2",
                            "target_identifier",
                            "external_id",
                        ],
                        "default_operator": "and",
                    }
                }
            ]
        else:
            must_query = [{"match_all": {}}]

        body = {
            "size": safe_limit,
            "query": {"bool": {"must": must_query, "filter": filters}},
            "sort": [{"observed_at": {"order": "desc", "missing": "_last"}}, {"event_id": {"order": "desc"}}],
            "highlight": {"fields": {"text": {}}, "fragment_size": 180, "number_of_fragments": 1},
        }
        response = client.search(index=self.index_name, body=body)
        hits_raw = (((response or {}).get("hits") or {}).get("hits") or [])
        total_raw = ((response or {}).get("hits") or {}).get("total") or 0
        if isinstance(total_raw, dict):
            total = int(total_raw.get("value") or 0)
        else:
            total = int(total_raw or 0)

        hits: list[dict[str, Any]] = []
        for item in hits_raw:
            source = item.get("_source") or {}
            highlight = item.get("highlight") or {}
            snippets = highlight.get("text") if isinstance(highlight, dict) else None
            snippet = snippets[0] if isinstance(snippets, list) and snippets else None
            hits.append(
                {
                    "event_id": int(source.get("event_id") or 0),
                    "parser_type": str(source.get("parser_type") or ""),
                    "target_id": int(source.get("target_id") or 0),
                    "target_name": str(source.get("target_name") or "-"),
                    "target_identifier": str(source.get("target_identifier") or "-"),
                    "account_id": int(source.get("account_id") or 0) if source.get("account_id") is not None else None,
                    "external_id": source.get("external_id"),
                    "event_type": str(source.get("event_type") or ""),
                    "is_comment": bool(source.get("is_comment")),
                    "sender_id": int(source.get("sender_id") or 0) if source.get("sender_id") is not None else None,
                    "sender_label": str(source.get("sender_label") or "-"),
                    "sender_username": source.get("sender_username"),
                    "text": str(source.get("text") or ""),
                    "snippet": snippet,
                    "observed_at": source.get("observed_at"),
                }
            )
        return hits, total

    def status(self) -> dict[str, Any]:
        enabled = self._enabled()
        package_installed = OpenSearch is not None
        reachable = False
        error = None
        if enabled and package_installed:
            client = self._get_client()
            if client is not None:
                try:
                    reachable = bool(client.ping())
                except Exception as exc:
                    error = str(exc)
        return {
            "enabled": bool(enabled),
            "package_installed": bool(package_installed),
            "reachable": bool(reachable),
            "index_name": self.index_name,
            "error": error,
        }


search_index = MessageSearchIndex()
