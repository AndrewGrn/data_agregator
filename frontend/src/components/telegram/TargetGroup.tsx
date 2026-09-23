import { useState } from "react";
import { Check, ChevronRight, Folder, FolderOpen, History, Pencil, Trash2, X, Zap } from "lucide-react";
import { TargetRow } from "./TargetRow";
import { IconButton } from "../ui/icon-button";
import { Input } from "../ui/input";
import {
  deleteTelegramGroup,
  renameTelegramGroup,
  setTelegramGroupMode,
  setTelegramTargetGroup,
  type TelegramTargetRow
} from "../../api/telegram";
import { cn } from "../../lib/utils";

/** id of the bucket for objects in no group. Dropping here clears the grouping. */
export const UNGROUPED = -1;

export function TargetGroup({
  id,
  name,
  rows,
  collapsed,
  onToggle,
  onChanged
}: {
  id: number;
  name: string;
  rows: TelegramTargetRow[];
  collapsed: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const [over, setOver] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const real = id !== UNGROUPED;

  const drop = async (event: React.DragEvent) => {
    event.preventDefault();
    setOver(false);
    const targetId = Number(event.dataTransfer.getData("text/telegram-target-id"));
    if (!targetId) return;
    // Dropping a row back into the group it already sits in is a no-op, not a request.
    if (rows.some((r) => r.id === targetId)) return;
    await setTelegramTargetGroup(targetId, real ? id : null);
    onChanged();
  };

  const commitRename = async () => {
    const next = (editing ?? "").trim();
    setEditing(null);
    if (!next || next === name) return;
    await renameTelegramGroup(id, next);
    onChanged();
  };

  const remove = async () => {
    const warning = rows.length
      ? `${rows.length} об'єкт(ів) перейдуть у «Без групи» і продовжать збиратися.`
      : "Група порожня.";
    if (!window.confirm(`Видалити групу «${name}»? ${warning}`)) return;
    await deleteTelegramGroup(id);
    onChanged();
  };

  const applyMode = async (mode: { live_enabled?: boolean; backfill_enabled?: boolean }) => {
    await setTelegramGroupMode(id, mode);
    onChanged();
  };

  const allLive = rows.length > 0 && rows.every((r) => r.live_enabled);
  const allBackfill = rows.length > 0 && rows.every((r) => r.backfill_state !== "off");
  const Icon = collapsed ? Folder : FolderOpen;

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div
        role={editing === null ? "button" : undefined}
        tabIndex={editing === null ? 0 : undefined}
        aria-expanded={!collapsed}
        onClick={editing === null ? onToggle : undefined}
        onKeyDown={(e) => {
          if (editing !== null) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        onDragOver={(e) => {
          // Without preventDefault the browser refuses the drop outright.
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => void drop(e)}
        className={cn(
          "group/header flex h-11 items-center gap-2 border-b border-border px-3 transition-colors",
          editing === null && "cursor-pointer hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none",
          over && "bg-primary/10 ring-1 ring-inset ring-primary"
        )}
      >
        <ChevronRight
          className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", !collapsed && "rotate-90")}
        />
        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />

        {editing !== null ? (
          <div className="flex min-w-0 flex-1 items-center gap-1" onClick={(e) => e.stopPropagation()}>
            <Input
              autoFocus
              value={editing}
              onChange={(e) => setEditing(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void commitRename();
                if (e.key === "Escape") setEditing(null);
              }}
              className="h-8 max-w-xs"
              aria-label="Нова назва групи"
            />
            <IconButton label="Зберегти" icon={Check} onClick={() => void commitRename()} />
            <IconButton label="Скасувати" icon={X} onClick={() => setEditing(null)} />
          </div>
        ) : (
          <>
            <span className={cn("truncate text-sm font-medium", !real && "text-muted-foreground")}>{name}</span>
            <span className="text-xs tabular-nums text-muted-foreground">{rows.length}</span>
            {over && <span className="text-xs text-primary">Перенести сюди</span>}
            {real && (
              <div
                className="ml-auto flex items-center gap-1 opacity-0 transition-opacity group-hover/header:opacity-100 focus-within:opacity-100"
                onClick={(e) => e.stopPropagation()}
              >
                <IconButton
                  label={allLive ? "Вимкнути реалтайм для всієї групи" : "Увімкнути реалтайм для всієї групи"}
                  icon={Zap}
                  variant={allLive ? "outline" : "ghost"}
                  disabled={rows.length === 0}
                  onClick={() => void applyMode({ live_enabled: !allLive })}
                />
                <IconButton
                  label={allBackfill ? "Вимкнути збір історії для всієї групи" : "Увімкнути збір історії для всієї групи"}
                  icon={History}
                  variant={allBackfill ? "outline" : "ghost"}
                  disabled={rows.length === 0}
                  onClick={() => void applyMode({ backfill_enabled: !allBackfill })}
                />
                <IconButton label="Перейменувати групу" icon={Pencil} onClick={() => setEditing(name)} />
                <IconButton
                  label="Видалити групу (об'єкти залишаться)"
                  icon={Trash2}
                  tone="danger"
                  onClick={() => void remove()}
                />
              </div>
            )}
          </>
        )}
      </div>

      {collapsed ? null : rows.length === 0 ? (
        <p className="px-3 py-4 text-sm text-muted-foreground">
          Група порожня — перетягніть сюди об'єкт або оберіть її під час додавання.
        </p>
      ) : (
        <div className="divide-y">
          {rows.map((row) => (
            <TargetRow key={row.id} row={row} onChanged={onChanged} draggable />
          ))}
        </div>
      )}
    </section>
  );
}
