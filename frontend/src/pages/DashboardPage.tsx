import { useEffect, useState } from "react";
import { apiGet } from "../api";

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

  useEffect(() => {
    let active = true;
    apiGet<{ modules: ModuleCard[] }>("/api/dashboard")
      .then((res) => {
        if (active) {
          setCards(res.modules);
        }
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <>
      <section className="card module-hero">
        <h2>Панель модулів</h2>
        <p className="hint">Огляд стану парсерів і зібраних даних.</p>
      </section>

      <section className="modules-grid">
        {cards.map((card) => (
          <article className="card module-card" key={card.parser_type}>
            <h3>{card.title}</h3>
            <div className="module-metrics module-metrics-wide">
              <div>
                <span>Цілі</span>
                <strong>{card.targets}</strong>
              </div>
              <div>
                <span>Акаунти</span>
                <strong>{card.accounts}</strong>
              </div>
              <div>
                <span>Активні задачі</span>
                <strong>{card.pending_jobs + card.running_jobs}</strong>
              </div>
              <div>
                <span>Raw події</span>
                <strong>{card.raw_events}</strong>
              </div>
            </div>
            <p className="hint">Остання подія: {card.last_event_text}</p>
          </article>
        ))}
        {!loading && cards.length === 0 ? <div className="card">Модулі не знайдені.</div> : null}
      </section>
    </>
  );
}
