import { Hash, Users, MessageCircle, Paperclip, Pause, Play, RefreshCw, Repeat, RotateCcw, Trash2 } from "lucide-react";
import { DataRow } from "../ui/data-row";
import { IconButton } from "../ui/icon-button";
import { StatusDot } from "../ui/status-dot";
import { StatusPill } from "../ui/status-pill";
import {
  deleteTelegramTarget,
  reassignTelegramTarget,
  retryTelegramOnboarding,
  runTelegramTargetNow,
  startTelegramTarget,
  stopTelegramTarget,
  updateTelegramTarget,
  type TelegramTargetRow,
} from "../../api/telegram";

const KIND_ICON = { channel: Hash, group: Users, private: MessageCircle } as const;

function relative(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "щойно";
  if (m < 60) return `${m} хв тому`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} год тому`;
  return `${Math.floor(h / 24)} д тому`;
}

export function statusOf(row: TelegramTargetRow): { tone: "ok" | "warn" | "bad" | "muted" | "info"; text: string; title?: string } {
  if (!row.is_active) return { tone: "muted", text: "Пауза" };
  switch (row.onboarding_step) {
    case "queued":
    case "resolving":
      return { tone: "info", text: "У черзі" };
    case "joining":
      return { tone: "info", text: "Вступає" };
    case "failed":
      return { tone: "bad", text: "Помилка", title: row.onboarding_error ?? undefined };
    case "pending_approval":
      return { tone: "warn", text: "Очікує схвалення", title: row.onboarding_error ?? undefined };
  }
  if (row.onboarding_status === "needs_account") return { tone: "warn", text: "Чекає акаунт" };
  if (row.onboarding_status === "blocked") return { tone: "warn", text: "Акаунти недоступні" };
  if (row.account && row.account.alive === false) return { tone: "bad", text: "Акаунт мертвий" };
  return { tone: "ok", text: "Моніториться" };
}

export function TargetRow({ row, onChanged }: { row: TelegramTargetRow; onChanged: () => void }) {
  const Icon = row.kind ? KIND_ICON[row.kind] : Hash;
  const status = statusOf(row);
  const inProgress = ["queued", "resolving", "joining"].includes(row.onboarding_step);

  const act = async (fn: () => Promise<unknown>) => {
    await fn();
    onChanged();
  };

  return (
    <DataRow
      leading={<Icon className="h-4 w-4 text-muted-foreground" aria-hidden />}
      primary={<span className="font-medium">{row.name}</span>}
      secondary={<span className="text-muted-foreground">{row.identifier}</span>}
      meta={
        <>
          <span title={status.title}>
            <StatusPill tone={status.tone}>{status.text}</StatusPill>
          </span>
          {row.account ? (
            <span className="inline-flex items-center gap-1.5 text-sm">
              <StatusDot tone={row.account.alive === false ? "bad" : row.account.alive ? "ok" : "muted"} />
              {row.account.label}
            </span>
          ) : (
            <span className="text-sm text-muted-foreground">без акаунта</span>
          )}
          <span className="text-sm text-muted-foreground">{relative(row.last_event_at)}</span>
          <span className="text-sm tabular-nums text-muted-foreground">{row.events_count}</span>
        </>
      }
      actions={
        <>
          {row.onboarding_step === "failed" && (
            <IconButton label="Повторити підключення" icon={RotateCcw} onClick={() => act(() => retryTelegramOnboarding(row.id))} />
          )}
          <IconButton
            label={row.is_active ? "Пауза" : "Відновити"}
            icon={row.is_active ? Pause : Play}
            onClick={() => act(() => (row.is_active ? stopTelegramTarget(row.id) : startTelegramTarget(row.id)))}
            disabled={inProgress}
          />
          <IconButton
            label={row.media_enabled ? "Медіа: вкл. Вимкнути завантаження файлів" : "Медіа: вимк. Завантажувати файли"}
            icon={Paperclip}
            variant={row.media_enabled ? "outline" : "ghost"}
            onClick={() => act(() => updateTelegramTarget(row.id, { media_enabled: !row.media_enabled }))}
          />
          <IconButton
            label="Бекфіл зараз"
            icon={RefreshCw}
            onClick={() => act(() => runTelegramTargetNow(row.id))}
            disabled={inProgress || !row.account}
          />
          <IconButton
            label="Переназначити акаунт"
            icon={Repeat}
            onClick={() => act(() => reassignTelegramTarget(row.id))}
            disabled={inProgress}
          />
          <IconButton
            label="Видалити"
            icon={Trash2}
            tone="danger"
            onClick={() => {
              if (!window.confirm(`Видалити «${row.name}»? Зібрані повідомлення залишаться в базі.`)) return;
              void act(() => deleteTelegramTarget(row.id));
            }}
          />
        </>
      }
    />
  );
}
