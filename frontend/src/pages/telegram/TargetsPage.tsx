import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FolderPlus, Radio, Settings } from "lucide-react";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { OnboardBar } from "../../components/telegram/OnboardBar";
import { TargetGroup, UNGROUPED } from "../../components/telegram/TargetGroup";
import {
  createTelegramGroup,
  fetchTelegramGroups,
  fetchTelegramModule,
  type TelegramGroup,
  type TelegramTargetRow
} from "../../api/telegram";

const IN_PROGRESS = new Set(["queued", "resolving", "joining"]);
const COLLAPSED_KEY = "telegram.collapsedGroups";

/** Collapsed groups are a per-viewer convenience; losing them must never break the page. */
function readCollapsed(): Set<number> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch {
    return new Set();
  }
}

function writeCollapsed(ids: Set<number>): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...ids]));
  } catch {
    /* private mode / blocked storage: collapsing just stops persisting */
  }
}

type Section = { id: number; name: string; rows: TelegramTargetRow[] };

/** Every group in its stored order, empty ones included, then the ungrouped bucket. */
export function buildSections(groups: TelegramGroup[], rows: TelegramTargetRow[]): Section[] {
  const byGroup = new Map<number, TelegramTargetRow[]>();
  for (const row of rows) {
    const key = row.group_id ?? UNGROUPED;
    const bucket = byGroup.get(key);
    if (bucket) bucket.push(row);
    else byGroup.set(key, [row]);
  }
  const sections: Section[] = groups.map((g) => ({ id: g.id, name: g.name, rows: byGroup.get(g.id) ?? [] }));
  const ungrouped = byGroup.get(UNGROUPED) ?? [];
  if (ungrouped.length > 0 || sections.length === 0) {
    sections.push({ id: UNGROUPED, name: "Без групи", rows: ungrouped });
  }
  return sections;
}

export function TargetsPage() {
  const [rows, setRows] = useState<TelegramTargetRow[]>([]);
  const [groups, setGroups] = useState<TelegramGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Set<number>>(readCollapsed);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    const [module, groupList] = await Promise.all([fetchTelegramModule(), fetchTelegramGroups()]);
    setRows(module.targets);
    setGroups(groupList.groups);
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

  const sections = useMemo(() => buildSections(groups, rows), [groups, rows]);

  const toggle = (id: number) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeCollapsed(next);
      return next;
    });
  };

  const addGroup = async () => {
    const name = window.prompt("Назва нової групи")?.trim();
    if (!name) return;
    setCreating(true);
    try {
      await createTelegramGroup(name);
      await load();
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Telegram</h1>
          <p className="text-sm text-muted-foreground">
            {rows.length} об'єктів під моніторингом
            {groups.length > 0 ? ` · ${groups.length} груп` : null}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" onClick={() => void addGroup()} disabled={creating}>
            <FolderPlus className="mr-2 h-4 w-4" />
            Нова група
          </Button>
          <Button asChild variant="outline">
            <Link to="/telegram/settings">
              <Settings className="mr-2 h-4 w-4" />
              Налаштування модуля
            </Link>
          </Button>
        </div>
      </header>

      <OnboardBar
        groups={groups}
        onQueued={(row) => setRows((prev) => [row, ...prev.filter((r) => r.id !== row.id)])}
      />

      {loading ? null : rows.length === 0 && groups.length === 0 ? (
        <EmptyState
          icon={Radio}
          title="Поки нічого не моніториться"
          hint="Вставте посилання на канал вище — решту система зробить сама."
        />
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-muted-foreground">
            Перетягніть рядок на заголовок групи, щоб перенести його. Клік по заголовку згортає групу, кнопки
            праворуч керують усією категорією.
          </p>
          {sections.map((section) => (
            <TargetGroup
              key={section.id}
              id={section.id}
              name={section.name}
              rows={section.rows}
              collapsed={collapsed.has(section.id)}
              onToggle={() => toggle(section.id)}
              onChanged={load}
            />
          ))}
        </div>
      )}
    </div>
  );
}
