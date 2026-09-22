import { useNavigate } from "react-router-dom";
import { Activity, ExternalLink, Power, Trash2, Users } from "lucide-react";
import { DataRow } from "../ui/data-row";
import { IconButton } from "../ui/icon-button";
import { StatusDot } from "../ui/status-dot";
import { StatusPill } from "../ui/status-pill";
import {
  checkTelegramAccount,
  deleteTelegramAccount,
  disableTelegramAccount,
  enableTelegramAccount,
  setTelegramAccountPoolMode,
  type TelegramAccountRow,
} from "../../api/telegram";

function ago(iso: string | null) {
  if (!iso) return "не перевірявся";
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "перевірено щойно" : `перевірено ${m} хв тому`;
}

export function AccountRow({ row, onChanged }: { row: TelegramAccountRow; onChanged: () => void }) {
  const navigate = useNavigate();
  const tone = row.alive === false ? "bad" : row.alive ? "ok" : "muted";
  const isShared = row.pool_mode === "shared";

  return (
    <DataRow
      leading={<Users className="h-4 w-4 text-muted-foreground" aria-hidden />}
      primary={<span className="font-medium">{row.label}</span>}
      secondary={<span className="text-muted-foreground">{row.username ? `@${row.username}` : "—"}</span>}
      meta={
        <>
          <button
            type="button"
            className="text-sm underline-offset-2 hover:underline"
            title={isShared ? "Бере канали з черги автоматично" : "Тільки ручне призначення"}
            onClick={async () => {
              await setTelegramAccountPoolMode(row.id, isShared ? "dedicated" : "shared");
              onChanged();
            }}
          >
            <StatusPill tone={isShared ? "info" : "muted"}>{isShared ? "Пул" : "Приватний"}</StatusPill>
          </button>
          <span className="inline-flex items-center gap-1.5 text-sm" title={row.dead_reason ?? undefined}>
            <StatusDot tone={tone} />
            {ago(row.last_checked_at)}
          </span>
          <span className="text-sm text-muted-foreground">{row.targets_count} каналів</span>
          <span className="text-sm tabular-nums text-muted-foreground">
            вступів {row.joins_today}/{row.join_daily_limit}
          </span>
        </>
      }
      actions={
        <>
          <IconButton
            label="Перевірити зараз"
            icon={Activity}
            onClick={async () => {
              await checkTelegramAccount(row.id);
              onChanged();
            }}
          />
          <IconButton label="Відкрити акаунт" icon={ExternalLink} onClick={() => navigate(`/telegram/accounts/${row.id}`)} />
          <IconButton
            label={row.is_active ? "Вимкнути акаунт" : "Увімкнути акаунт"}
            icon={Power}
            tone={row.is_active ? "danger" : "default"}
            onClick={async () => {
              if (row.is_active) {
                await disableTelegramAccount(row.id);
              } else {
                await enableTelegramAccount(row.id);
              }
              onChanged();
            }}
          />
          <IconButton
            label="Видалити акаунт"
            icon={Trash2}
            tone="danger"
            onClick={async () => {
              if (!window.confirm(`Видалити акаунт «${row.label}»? Це неможливо скасувати.`)) return;
              try {
                await deleteTelegramAccount(row.id);
                onChanged();
              } catch (err) {
                window.alert(err instanceof Error ? err.message : "Не вдалося видалити акаунт");
              }
            }}
          />
        </>
      }
    />
  );
}
