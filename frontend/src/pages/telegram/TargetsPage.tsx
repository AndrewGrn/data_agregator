import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Radio, Settings } from "lucide-react";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { OnboardBar } from "../../components/telegram/OnboardBar";
import { TargetRow } from "../../components/telegram/TargetRow";
import { fetchTelegramModule, type TelegramTargetRow } from "../../api/telegram";

const IN_PROGRESS = new Set(["queued", "resolving", "joining"]);

export function TargetsPage() {
  const [rows, setRows] = useState<TelegramTargetRow[]>([]);
  const [loading, setLoading] = useState(true);

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

  return (
    <div className="flex flex-col gap-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Telegram</h1>
          <p className="text-sm text-muted-foreground">{rows.length} об'єктів під моніторингом</p>
        </div>
        <Button asChild variant="outline">
          <Link to="/telegram/settings">
            <Settings className="mr-2 h-4 w-4" />
            Налаштування модуля
          </Link>
        </Button>
      </header>

      <OnboardBar onQueued={(row) => setRows((prev) => [row, ...prev.filter((r) => r.id !== row.id)])} />

      {loading ? null : rows.length === 0 ? (
        <EmptyState icon={Radio} title="Поки нічого не моніториться" hint="Вставте посилання на канал вище — решту система зробить сама." />
      ) : (
        <div className="divide-y rounded-lg border bg-card">
          {rows.map((row) => (
            <TargetRow key={row.id} row={row} onChanged={load} />
          ))}
        </div>
      )}
    </div>
  );
}
