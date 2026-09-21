import { Link } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../api";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";

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

  return (
    <>
      <Card>
        <CardHeader className="border-b">
          <CardTitle className="text-xl">Панель модулів</CardTitle>
          <CardDescription>Огляд стану парсерів і зібраних даних.</CardDescription>
        </CardHeader>
      </Card>

      <section className="grid gap-4 md:grid-cols-2">
        {cards.map((card) => (
          <Card key={card.parser_type} className="relative overflow-hidden">
            <div className="absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-[#2554B1] to-[#2162C7]" />
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-lg">{card.title}</CardTitle>
                <Badge variant={card.failed_jobs > 0 ? "destructive" : "secondary"}>
                  {card.failed_jobs > 0 ? `Помилки: ${card.failed_jobs}` : "Стабільно"}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <div className="rounded-md border bg-gradient-to-br from-blue-50 to-blue-100 p-3">
                  <p className="text-xs text-muted-foreground">Цілі</p>
                  <p className="text-xl font-semibold">{card.targets}</p>
                </div>
                <div className="rounded-md border bg-gradient-to-br from-indigo-50 to-indigo-100 p-3">
                  <p className="text-xs text-muted-foreground">Акаунти</p>
                  <p className="text-xl font-semibold">{card.accounts}</p>
                </div>
                <div className="rounded-md border bg-gradient-to-br from-sky-50 to-sky-100 p-3">
                  <p className="text-xs text-muted-foreground">Активні задачі</p>
                  <p className="text-xl font-semibold">{card.pending_jobs + card.running_jobs}</p>
                </div>
                <div className="rounded-md border bg-gradient-to-br from-cyan-50 to-cyan-100 p-3">
                  <p className="text-xs text-muted-foreground">Події</p>
                  <p className="text-xl font-semibold">{card.raw_events}</p>
                </div>
              </div>
              <p className="text-sm text-muted-foreground">Остання подія: {card.last_event_text}</p>
              <Button asChild type="button" variant="outline" size="sm">
                <Link to={card.parser_type === "darknet" ? "/darknet" : "/telegram"}>Відкрити модуль</Link>
              </Button>
            </CardContent>
          </Card>
        ))}
        {!loading && cards.length === 0 ? (
          <Card>
            <CardContent className="pt-6 text-sm text-muted-foreground">Модулі не знайдені.</CardContent>
          </Card>
        ) : null}
      </section>
    </>
  );
}
