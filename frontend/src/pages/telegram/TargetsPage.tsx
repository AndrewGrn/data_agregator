import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Radio, Settings } from "lucide-react";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { OnboardBar } from "../../components/telegram/OnboardBar";
import { TargetGroup, UNGROUPED } from "../../components/telegram/TargetGroup";
import { fetchTelegramModule, type TelegramTargetRow } from "../../api/telegram";

const IN_PROGRESS = new Set(["queued", "resolving", "joining"]);
const COLLAPSED_KEY = "telegram.collapsedGroups";

/** Collapsed groups are a per-viewer convenience; losing them must never break the page. */
function readCollapsed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

function writeCollapsed(names: Set<string>): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...names]));
  } catch {
    /* private mode / blocked storage: collapsing just stops persisting */
  }
}

/** Named groups first, alphabetically; the ungrouped bucket always last. */
export function groupTargets(rows: TelegramTargetRow[]): [string, TelegramTargetRow[]][] {
  const byName = new Map<string, TelegramTargetRow[]>();
  for (const row of rows) {
    const key = row.group_name?.trim() || UNGROUPED;
    const bucket = byName.get(key);
    if (bucket) bucket.push(row);
    else byName.set(key, [row]);
  }
  return [...byName.entries()].sort(([a], [b]) => {
    if (a === UNGROUPED) return 1;
    if (b === UNGROUPED) return -1;
    return a.localeCompare(b);
  });
}

export function TargetsPage() {
  const [rows, setRows] = useState<TelegramTargetRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed);

  const load = useCallback(async () => {
    const data = await fetchTelegramModule();
    setRows(data.targets);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const busy = useMemo(() => rows.some((r) => IN_PROGRESS.has(r.onboarding_step)), [rows]);
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => {
      void load();
    }, 5000);
    return () => window.clearInterval(t);
  }, [busy, load]);

  const groups = useMemo(() => groupTargets(rows), [rows]);
  const groupNames = useMemo(
    () => groups.map(([name]) => name).filter((name) => name !== UNGROUPED),
    [groups]
  );

  const toggle = (name: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      writeCollapsed(next);
      return next;
    });
  };

  return (
    <div className="flex flex-col gap-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Telegram</h1>
          <p className="text-sm text-muted-foreground">
            {rows.length} об'єктів під моніторингом
            {groupNames.length > 0 ? ` · ${groupNames.length} груп` : null}
          </p>
        </div>
        <Button asChild variant="outline">
          <Link to="/telegram/settings">
            <Settings className="mr-2 h-4 w-4" />
            Налаштування модуля
          </Link>
        </Button>
      </header>

      <OnboardBar
        groups={groupNames}
        onQueued={(row) => setRows((prev) => [row, ...prev.filter((r) => r.id !== row.id)])}
      />

      {loading ? null : rows.length === 0 ? (
        <EmptyState
          icon={Radio}
          title="Поки нічого не моніториться"
          hint="Вставте посилання на канал вище — решту система зробить сама."
        />
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-muted-foreground">
            Перетягніть рядок на заголовок групи, щоб перенести його. Клік по заголовку згортає групу.
          </p>
          {groups.map(([name, groupRows]) => (
            <TargetGroup
              key={name}
              name={name}
              rows={groupRows}
              collapsed={collapsed.has(name)}
              onToggle={() => toggle(name)}
              onChanged={load}
            />
          ))}
        </div>
      )}
    </div>
  );
}
