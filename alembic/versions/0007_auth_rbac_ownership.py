"""Add auth RBAC, registration tokens and ownership bindings

Revision ID: 0007_auth_rbac_ownership
Revises: 0006_tg_offsets_priority
Create Date: 2026-03-24 16:20:00

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007_auth_rbac_ownership"
down_revision: Union[str, Sequence[str], None] = "0006_tg_offsets_priority"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


user_role_enum = sa.Enum("admin", "user", name="userrole")


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, table_name) and not _has_column(insp, table_name, str(column.name)):
        op.add_column(table_name, column)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, table_name) and _has_index(insp, table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, table_name) and _has_column(insp, table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if bind.dialect.name != "sqlite":
        user_role_enum.create(bind, checkfirst=True)

    # users table extensions
    _add_column_if_missing("users", sa.Column("role", user_role_enum, nullable=True))
    _add_column_if_missing("users", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    _add_column_if_missing("users", sa.Column("totp_secret", sa.String(length=64), nullable=True))
    _add_column_if_missing("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    _add_column_if_missing("users", sa.Column("totp_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    _add_column_if_missing("users", sa.Column("created_by_token_id", sa.Integer(), nullable=True))

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "users") and not _has_index(insp, "users", "ix_users_role"):
        op.create_index("ix_users_role", "users", ["role"], unique=False)
    if _has_table(insp, "users") and not _has_index(insp, "users", "ix_users_created_by_token_id"):
        op.create_index("ix_users_created_by_token_id", "users", ["created_by_token_id"], unique=False)

    # registration tokens table
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not _has_table(insp, "registration_tokens"):
        op.create_table(
            "registration_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("label", sa.String(length=128), nullable=True),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), nullable=False),
            sa.Column("used_by_user_id", sa.Integer(), nullable=True),
            sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["used_by_user_id"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_registration_tokens_token_hash", "registration_tokens", ["token_hash"], unique=True)
        op.create_index("ix_registration_tokens_created_by_user_id", "registration_tokens", ["created_by_user_id"], unique=False)
        op.create_index("ix_registration_tokens_used_by_user_id", "registration_tokens", ["used_by_user_id"], unique=False)
        op.create_index("ix_registration_tokens_created_at", "registration_tokens", ["created_at"], unique=False)

    # wire users.created_by_token_id fk after table exists
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if bind.dialect.name != "sqlite" and _has_table(insp, "users") and _has_table(insp, "registration_tokens"):
        fk_names = {fk.get("name") for fk in insp.get_foreign_keys("users")}
        if "fk_users_created_by_token_id" not in fk_names:
            op.create_foreign_key(
                "fk_users_created_by_token_id",
                "users",
                "registration_tokens",
                ["created_by_token_id"],
                ["id"],
                ondelete="SET NULL",
            )

    # ownership columns
    _add_column_if_missing("parser_accounts", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    _add_column_if_missing("targets", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    _add_column_if_missing("target_account_links", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    _add_column_if_missing("parse_jobs", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    _add_column_if_missing("raw_events", sa.Column("owner_user_id", sa.Integer(), nullable=True))

    bind = op.get_bind()
    insp = sa.inspect(bind)
    ownership_indexes = [
        ("parser_accounts", "ix_parser_accounts_owner_user_id"),
        ("targets", "ix_targets_owner_user_id"),
        ("target_account_links", "ix_target_account_links_owner_user_id"),
        ("parse_jobs", "ix_parse_jobs_owner_user_id"),
        ("raw_events", "ix_raw_events_owner_user_id"),
    ]
    for table_name, index_name in ownership_indexes:
        if _has_table(insp, table_name) and not _has_index(insp, table_name, index_name):
            op.create_index(index_name, table_name, ["owner_user_id"], unique=False)

    # best-effort foreign keys (some sqlite variants don't support ALTER TABLE add FK reliably)
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if bind.dialect.name != "sqlite":
        fk_map = [
            ("parser_accounts", "fk_parser_accounts_owner_user_id"),
            ("targets", "fk_targets_owner_user_id"),
            ("target_account_links", "fk_target_account_links_owner_user_id"),
            ("parse_jobs", "fk_parse_jobs_owner_user_id"),
            ("raw_events", "fk_raw_events_owner_user_id"),
        ]
        for table_name, fk_name in fk_map:
            fk_names = {fk.get("name") for fk in insp.get_foreign_keys(table_name)} if _has_table(insp, table_name) else set()
            if fk_name not in fk_names:
                op.create_foreign_key(
                    fk_name,
                    table_name,
                    "users",
                    ["owner_user_id"],
                    ["id"],
                    ondelete="SET NULL",
                )

    # normalize current data
    bind = op.get_bind()
    admin_id = bind.execute(
        sa.text(
            "SELECT id FROM users ORDER BY CASE WHEN is_admin = 1 THEN 0 ELSE 1 END, id ASC LIMIT 1"
        )
    ).scalar()
    if admin_id is None:
        admin_id = 1

    bind.execute(sa.text("UPDATE users SET role = CASE WHEN is_admin = 1 THEN 'admin' ELSE 'user' END WHERE role IS NULL"))
    bind.execute(sa.text("UPDATE users SET is_active = 1 WHERE is_active IS NULL"))
    bind.execute(sa.text("UPDATE users SET totp_enabled = 1 WHERE totp_enabled IS NULL"))
    bind.execute(sa.text("UPDATE users SET totp_confirmed = 0 WHERE totp_confirmed IS NULL"))

    bind.execute(sa.text("UPDATE parser_accounts SET owner_user_id = :admin_id WHERE owner_user_id IS NULL"), {"admin_id": admin_id})
    bind.execute(sa.text("UPDATE targets SET owner_user_id = :admin_id WHERE owner_user_id IS NULL"), {"admin_id": admin_id})
    bind.execute(
        sa.text(
            """
            UPDATE target_account_links
            SET owner_user_id = (
                SELECT t.owner_user_id FROM targets t WHERE t.id = target_account_links.target_id
            )
            WHERE owner_user_id IS NULL
            """
        )
    )
    bind.execute(sa.text("UPDATE target_account_links SET owner_user_id = :admin_id WHERE owner_user_id IS NULL"), {"admin_id": admin_id})

    bind.execute(
        sa.text(
            """
            UPDATE parse_jobs
            SET owner_user_id = (
                SELECT t.owner_user_id FROM targets t WHERE t.id = parse_jobs.target_id
            )
            WHERE owner_user_id IS NULL
            """
        )
    )
    bind.execute(sa.text("UPDATE parse_jobs SET owner_user_id = :admin_id WHERE owner_user_id IS NULL"), {"admin_id": admin_id})

    bind.execute(
        sa.text(
            """
            UPDATE raw_events
            SET owner_user_id = (
                SELECT t.owner_user_id FROM targets t WHERE t.id = raw_events.target_id
            )
            WHERE owner_user_id IS NULL
            """
        )
    )
    bind.execute(sa.text("UPDATE raw_events SET owner_user_id = :admin_id WHERE owner_user_id IS NULL"), {"admin_id": admin_id})



def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    for table_name, index_name in [
        ("raw_events", "ix_raw_events_owner_user_id"),
        ("parse_jobs", "ix_parse_jobs_owner_user_id"),
        ("target_account_links", "ix_target_account_links_owner_user_id"),
        ("targets", "ix_targets_owner_user_id"),
        ("parser_accounts", "ix_parser_accounts_owner_user_id"),
    ]:
        _drop_index_if_exists(table_name, index_name)

    for table_name, column_name in [
        ("raw_events", "owner_user_id"),
        ("parse_jobs", "owner_user_id"),
        ("target_account_links", "owner_user_id"),
        ("targets", "owner_user_id"),
        ("parser_accounts", "owner_user_id"),
    ]:
        _drop_column_if_exists(table_name, column_name)

    _drop_index_if_exists("users", "ix_users_created_by_token_id")
    _drop_index_if_exists("users", "ix_users_role")

    # users FK to registration tokens
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if bind.dialect.name != "sqlite" and _has_table(insp, "users"):
        fk_names = {fk.get("name") for fk in insp.get_foreign_keys("users")}
        if "fk_users_created_by_token_id" in fk_names:
            op.drop_constraint("fk_users_created_by_token_id", "users", type_="foreignkey")

    for column_name in [
        "created_by_token_id",
        "totp_confirmed",
        "totp_enabled",
        "totp_secret",
        "is_active",
        "role",
    ]:
        _drop_column_if_exists("users", column_name)

    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _has_table(insp, "registration_tokens"):
        _drop_index_if_exists("registration_tokens", "ix_registration_tokens_created_at")
        _drop_index_if_exists("registration_tokens", "ix_registration_tokens_used_by_user_id")
        _drop_index_if_exists("registration_tokens", "ix_registration_tokens_created_by_user_id")
        _drop_index_if_exists("registration_tokens", "ix_registration_tokens_token_hash")
        op.drop_table("registration_tokens")

    if bind.dialect.name != "sqlite":
        user_role_enum.drop(bind, checkfirst=True)
