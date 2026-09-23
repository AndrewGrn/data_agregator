import { useState } from "react";
import { ChevronRight, Folder, FolderOpen } from "lucide-react";
import { TargetRow } from "./TargetRow";
import { setTelegramTargetGroup, type TelegramTargetRow } from "../../api/telegram";
import { cn } from "../../lib/utils";

/** The bucket for targets that belong to no group. Dropping here clears the group. */
export const UNGROUPED = "\u0000ungrouped";

export function groupLabel(name: string): string {
  return name === UNGROUPED ? "Без групи" : name;
}

export function TargetGroup({
  name,
  rows,
  collapsed,
  onToggle,
  onChanged
}: {
  name: string;
  rows: TelegramTargetRow[];
  collapsed: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const [over, setOver] = useState(false);

  const drop = async (event: React.DragEvent) => {
    event.preventDefault();
    setOver(false);
    const id = Number(event.dataTransfer.getData("text/telegram-target-id"));
    if (!id) return;
    // Dropping a row back into the group it already sits in is a no-op, not a request.
    if (rows.some((r) => r.id === id)) return;
    await setTelegramTargetGroup(id, name === UNGROUPED ? null : name);
    onChanged();
  };

  const Icon = collapsed ? Folder : FolderOpen;

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div
        role="button"
        tabIndex={0}
        aria-expanded={!collapsed}
        onClick={onToggle}
        onKeyDown={(e) => {
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
          "flex h-11 cursor-pointer items-center gap-2 border-b border-border px-3 transition-colors hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none",
          over && "bg-primary/10 ring-1 ring-inset ring-primary"
        )}
      >
        <ChevronRight className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", !collapsed && "rotate-90")} />
        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        <span className={cn("truncate text-sm font-medium", name === UNGROUPED && "text-muted-foreground")}>
          {groupLabel(name)}
        </span>
        <span className="text-xs tabular-nums text-muted-foreground">{rows.length}</span>
        {over && <span className="ml-auto text-xs text-primary">Перенести сюди</span>}
      </div>

      {collapsed ? null : (
        <div className="divide-y">
          {rows.map((row) => (
            <TargetRow key={row.id} row={row} onChanged={onChanged} draggable />
          ))}
        </div>
      )}
    </section>
  );
}
