from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ParseJob, ParserAccount, Target, TargetAccountLink
from app.plugins.base import FileRef, JobSpec, ParsedEvent, ParserPlugin

logger = logging.getLogger(__name__)

QUEUE_WHATSAPP = "whatsapp"


def map_wa_event(message: dict) -> ParsedEvent:
    """Map a bridge message (contract v1) onto a ParsedEvent."""
    author_id = str(message.get("author") or message.get("from") or "").strip() or None

    author_label = str(message.get("author_name") or "").strip()
    if not author_label and author_id:
        # "380671234567@c.us" reads better as the bare number
        author_label = author_id.split("@", 1)[0]

    timestamp = message.get("timestamp")
    observed_at = (
        dt.datetime.fromtimestamp(int(timestamp), dt.UTC) if timestamp is not None else None
    )

    media = message.get("media") if isinstance(message.get("media"), dict) else None
    files: list[FileRef] = []
    if media and media.get("sha256"):
        files.append(
            FileRef(
                source_ref=str(message.get("message_id") or ""),
                filename=media.get("filename"),
                mime=media.get("mime"),
                size=media.get("size"),
                sha256=str(media["sha256"]),
            )
        )

    has_quoted = bool(message.get("has_quoted"))
    raw = message.get("raw")

    return ParsedEvent(
        external_id=str(message.get("message_id") or "") or None,
        observed_at=observed_at,
        payload=raw if isinstance(raw, dict) else {},
        text=str(message.get("body") or "") or None,
        author_id=author_id,
        author_label=author_label or None,
        event_kind="reply" if has_quoted else "message",
        thread_id=str(message.get("chat_id") or "") or None,
        reply_to=str(message.get("quoted_message_id") or "") or None,
        files=files,
    )


class WhatsAppPlugin(ParserPlugin):
    parser_type = "whatsapp"

    def generate_jobs(self, session: Session, target: Target) -> list[JobSpec]:
        """One backfill job per target; live messages arrive through the stream."""
        limit = int((target.config or {}).get("backfill_limit") or 200)
        return [
            JobSpec(
                parser_type=self.parser_type,
                target_id=target.id,
                account_id=None,
                job_key=f"wa-backfill:{target.id}",
                payload={"chat_id": target.identifier, "limit": limit},
                queue=QUEUE_WHATSAPP,
            )
        ]

    def run(
        self,
        session: Session,
        job: ParseJob,
        target: Target,
        account: ParserAccount | None,
    ) -> list[ParsedEvent]:
        """Ask the bridge for history; events arrive via the listener, not here."""
        from app.services.whatsapp_bus import request_backfill

        request_backfill(
            account_id=account.id if account else None,
            chat_id=str(target.identifier),
            limit=int((job.payload or {}).get("limit") or 200),
        )
        return []

    def sync_memberships(self, session: Session) -> dict:
        """Refresh group membership for every active WhatsApp target.

        Only `@g.us` chats (groups) have a member list; `@c.us` one-to-one
        chats are skipped rather than asked, since the question is
        meaningless for them.
        """
        from app.services.whatsapp_bus import request_participants

        targets = (
            session.execute(
                select(Target).where(
                    Target.parser_type == "whatsapp",
                    Target.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )

        checked = 0
        linked = 0
        for target in targets:
            if not str(target.identifier or "").endswith("@g.us"):
                continue  # direct chats have no participant list

            link = session.execute(
                select(TargetAccountLink).where(TargetAccountLink.target_id == target.id)
            ).scalars().first()
            if link is None:
                logger.info("skipping membership sync for target %s: no linked account", target.id)
                continue

            members = request_participants(account_id=link.account_id, chat_id=target.identifier)
            checked += 1
            linked += len(members)

        return {"checked": checked, "linked": linked}
