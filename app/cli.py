import asyncio

import click
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import get_settings
from app.db import session_scope
from app.services.bootstrap import ensure_default_admin, run_migrations
from app.services.telegram_listener import run_telegram_listener_forever
from app.services.scheduler import (
    run_scheduler_forever,
    schedule_once,
    sync_telegram_memberships,
    sync_whatsapp_memberships,
)
from app.services.worker import run_workers

settings = get_settings()


def _alembic_config() -> AlembicConfig:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


@click.group()
def cli() -> None:
    pass


@cli.command("init-db")
def init_db_cmd() -> None:
    run_migrations()
    with session_scope() as session:
        ensure_default_admin(session)
    click.echo("Міграції застосовано. Адмін-користувач перевірений.")


@cli.command("run-scheduler")
@click.option("--loop", "loop_mode", is_flag=True, default=False)
def run_scheduler_cmd(loop_mode: bool) -> None:
    if loop_mode:
        run_scheduler_forever(session_scope, settings.worker_poll_interval)
        return

    with session_scope() as session:
        result = schedule_once(session)
    click.echo(result)


@cli.command("sync-telegram-memberships")
def sync_telegram_cmd() -> None:
    with session_scope() as session:
        result = sync_telegram_memberships(session)
    click.echo(result)


@cli.command("sync-whatsapp-memberships")
def sync_whatsapp_cmd() -> None:
    with session_scope() as session:
        result = sync_whatsapp_memberships(session)
    click.echo(result)


@cli.command("run-worker")
@click.option("--concurrency", default=2, type=int, show_default=True)
@click.option(
    "--queues",
    default="",
    type=str,
    show_default=False,
    help="Comma-separated queues (e.g. telegram_live,telegram_backfill,darknet,web). Empty = all queues.",
)
def run_worker_cmd(concurrency: int, queues: str) -> None:
    queue_set = {item.strip().lower() for item in str(queues or "").split(",") if item.strip()}
    run_workers(session_scope, concurrency, queue_set or None)


@cli.command("run-telegram-listener")
@click.option(
    "--refresh-seconds",
    default=settings.telegram_listener_refresh_seconds,
    type=int,
    show_default=True,
)
def run_telegram_listener_cmd(refresh_seconds: int) -> None:
    run_telegram_listener_forever(session_scope, refresh_seconds=max(int(refresh_seconds), 5))


@cli.command("run-whatsapp-listener")
def run_whatsapp_listener_cmd() -> None:
    from app.services.whatsapp_listener import run_whatsapp_listener

    run_whatsapp_listener()


@cli.command("db-upgrade")
def db_upgrade_cmd() -> None:
    run_migrations()
    click.echo("Alembic upgrade head виконано.")


@cli.command("db-revision")
@click.option("--message", required=True, type=str, help="Опис змін для ревізії")
@click.option("--autogenerate", is_flag=True, default=False)
def db_revision_cmd(message: str, autogenerate: bool) -> None:
    alembic_command.revision(_alembic_config(), message=message, autogenerate=autogenerate)
    click.echo("Нова ревізія створена.")


@cli.command("ch-backfill")
@click.option("--batch-size", default=5000, type=int, show_default=True)
def ch_backfill_cmd(batch_size: int) -> None:
    """Copy every raw_events row into ClickHouse (idempotent)."""
    from app.db import SessionLocal
    from app.services.clickhouse_store import backfill_from_postgres

    with SessionLocal() as session:
        total = backfill_from_postgres(session, batch_size=batch_size)
    click.echo(f"ClickHouse backfill: {total} rows")


@cli.command("generate-telegram-session")
@click.option("--api-id", required=True, type=int)
@click.option("--api-hash", required=True, type=str)
@click.option("--phone", required=True, type=str)
def generate_telegram_session_cmd(api_id: int, api_hash: str, phone: str) -> None:
    async def _run() -> None:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = click.prompt("Enter Telegram code")
            try:
                await client.sign_in(phone=phone, code=code)
            except Exception:
                password = click.prompt("Enter 2FA password", hide_input=True)
                await client.sign_in(password=password)

        session_string = client.session.save()
        await client.disconnect()
        click.echo("Session string:")
        click.echo(session_string)

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
