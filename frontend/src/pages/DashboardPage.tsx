import { Link } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { apiGet } from "../api";
import { Button } from "../components/ui/button";
import { Card, CardContent } from "../components/ui/card";
import { StatusPill } from "../components/ui/status-pill";

type ModuleCard = {
  parser_type: string;
  title: string;
  targets: number;
  accounts: number;
  pending_jobs: number;
  running_jobs: number;
  failed_jobs: number;
  raw_events: number;
  last_event_text: string;
};

const MODULE_PATH: Record<string, string> = {
  telegram: "/telegram",
  whatsapp: "/whatsapp",
  darknet: "/darknet",
};

function Metric({ label, value, tone }: { label: string; value: number; tone?: "bad" }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={`text-2xl font-semibold tabular-nums ${tone === "bad" ? "text-destructive" : ""}`}>
        {value}
      </span>
    </div>
  );
}

export function DashboardPage() {
  const [cards, setCards] = useState<ModuleCard[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await apiGet<{ modules: ModuleCard[] }>("/api/dashboard");
      setCards(res.modules);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      void load();
    }, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const totals = cards.reduce(
    (acc, c) => ({
      targets: acc.targets + c.targets,
      accounts: acc.accounts + c.accounts,
      active: acc.active + c.pending_jobs + c.running_jobs,
      events: acc.events + c.raw_events,
      failed: acc.failed + c.failed_jobs,
    }),
    { targets: 0, accounts: 0, active: 0, events: 0, failed: 0 },
  );

  return (
    <div className="flex flex-col gap-5">
      <section className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-5">
        {[
          { label: "Об'єктів", value: totals.targets },
          { label: "Акаунтів", value: totals.accounts },
          { label: "Активних задач", value: totals.active },
          { label: "Подій", value: totals.events },
          { label: "Помилок", value: totals.failed, tone: totals.failed > 0 ? ("bad" as const) : undefined },
        ].map((m) => (
          <div key={m.label} className="bg-card px-4 py-3">
            <Metric label={m.label} value={m.value} tone={m.tone} />
          </div>
        ))}
      </section>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {cards.map((card) => {
          const active = card.pending_jobs + card.running_jobs;
          return (
            <Card key={card.parser_type} className="flex flex-col">
              <CardContent className="flex flex-1 flex-col gap-4 p-4">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="text-base font-semibold leading-tight">{card.title}</h2>
                  <StatusPill tone={card.failed_jobs > 0 ? "bad" : "ok"}>
                    {card.failed_jobs > 0 ? `Помилок: ${card.failed_jobs}` : "Стабільно"}
                  </StatusPill>
                </div>

                <div className="grid grid-cols-4 gap-3">
                  <Metric label="Об'єкти" value={card.targets} />
                  <Metric label="Акаунти" value={card.accounts} />
                  <Metric label="Задачі" value={active} />
                  <Metric label="Події" value={card.raw_events} />
                </div>

                <div className="mt-auto flex items-center justify-between gap-2 border-t pt-3">
                  <span className="truncate text-xs text-muted-foreground" title={card.last_event_text}>
                    Остання подія: {card.last_event_text}
                  </span>
                  <Button asChild type="button" variant="ghost" size="sm" className="shrink-0">
                    <Link to={MODULE_PATH[card.parser_type] ?? "/telegram"}>
                      Відкрити <ArrowRight className="ml-1 h-3.5 w-3.5" />
                    </Link>
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
        {!loading && cards.length === 0 ? (
          <p className="text-sm text-muted-foreground">Модулі не знайдені.</p>
        ) : null}
      </section>
    </div>
  );
}
