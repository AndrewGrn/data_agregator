import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiGet, apiPost } from "../api";

type TelegramTarget = {
  id: number;
  name: string;
  identifier: string;
  is_active: boolean;
  running: number;
  queued: number;
  failed: number;
  events_count: number;
  last_success_text: string;
  process_text: string;
  ingest_mode: string;
  offset_max_message_id: number;
  offset_accounts_text: string;
};

type TelegramAccount = {
  id: number;
  label: string;
  is_active: boolean;
  utilization: number;
  queued_jobs: number;
  dialogs_count: number;
  parallel_jobs: number;
  backfill_parallel_jobs: number;
  last_success_text: string;
};

type TelegramJob = {
  id: number;
  target_id: number;
  target_name: string;
  account_id: number | null;
  account_label: string;
  status: string;
  job_type: string;
  attempt: number;
  error: string | null;
  wait_reason: string;
};

type TelegramModuleResponse = {
  summary: {
    targets: number;
    accounts: number;
    events_count: number;
    problem_jobs: number;
  };
  targets: TelegramTarget[];
  accounts: TelegramAccount[];
  jobs: TelegramJob[];
};

export function TelegramPage() {
  const [data, setData] = useState<TelegramModuleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pendingTargetId, setPendingTargetId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const response = await apiGet<TelegramModuleResponse>("/api/modules/telegram");
      setData(response);
      setError("");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити Telegram модуль");
      }
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

  const callTargetAction = async (targetId: number, action: "run-now" | "stop" | "start") => {
    setPendingTargetId(targetId);
    setError("");
    try {
      await apiPost<{ ok: boolean }>(`/api/modules/telegram/targets/${targetId}/${action}`);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося виконати дію");
      }
    } finally {
      setPendingTargetId(null);
    }
  };

  const activeJobs = useMemo(() => data?.jobs.filter((job) => job.status !== "succeeded") ?? [], [data?.jobs]);

  return (
    <>
      <section className="card module-hero">
        <h2>Telegram модуль (React)</h2>
        <p className="hint">Керування парсингом Telegram у React-інтерфейсі.</p>
        {error ? <div className="error-box">{error}</div> : null}
        <div className="module-metrics module-metrics-wide">
          <div>
            <span>Цілей</span>
            <strong>{data?.summary.targets ?? 0}</strong>
          </div>
          <div>
            <span>Акаунтів</span>
            <strong>{data?.summary.accounts ?? 0}</strong>
          </div>
          <div>
            <span>Збережено повідомлень</span>
            <strong>{data?.summary.events_count ?? 0}</strong>
          </div>
          <div>
            <span>Проблемних задач</span>
            <strong>{data?.summary.problem_jobs ?? 0}</strong>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-head">
          <h3>Список каналів і чатів</h3>
          <button type="button" onClick={() => void load()} disabled={loading}>
            Оновити
          </button>
        </div>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Назва</th>
              <th>Канал / чат</th>
              <th>Стан</th>
              <th>Процес парсингу</th>
              <th>Збережено повідомлень</th>
              <th>Останній успішний запуск</th>
              <th>Дії</th>
            </tr>
          </thead>
          <tbody>
            {data?.targets.map((target) => (
              <tr key={target.id}>
                <td>{target.id}</td>
                <td>{target.name}</td>
                <td>{target.identifier}</td>
                <td>{target.is_active ? "Увімкнено" : "Вимкнено"}</td>
                <td>
                  <div>{target.process_text}</div>
                  <div className="hint">Режим: {target.ingest_mode}</div>
                  <div className="hint">Offset: {target.offset_max_message_id} | Синхронно: {target.offset_accounts_text}</div>
                </td>
                <td>{target.events_count}</td>
                <td>{target.last_success_text}</td>
                <td>
                  <div className="action-row">
                    <button
                      className="icon-btn"
                      type="button"
                      disabled={pendingTargetId === target.id}
                      onClick={() => void callTargetAction(target.id, "run-now")}
                      title="Оновити зараз"
                    >
                      ↻
                    </button>
                    {target.is_active ? (
                      <button
                        className="icon-btn danger"
                        type="button"
                        disabled={pendingTargetId === target.id}
                        onClick={() => void callTargetAction(target.id, "stop")}
                        title="Зупинити"
                      >
                        ⏹
                      </button>
                    ) : (
                      <button
                        className="icon-btn"
                        type="button"
                        disabled={pendingTargetId === target.id}
                        onClick={() => void callTargetAction(target.id, "start")}
                        title="Увімкнути"
                      >
                        ▶
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {data && data.targets.length === 0 ? (
              <tr>
                <td colSpan={8}>Цілі відсутні.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>

      <section className="grid-2">
        <article className="card">
          <h3>Акаунти</h3>
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Мітка</th>
                <th>Завантаженість</th>
                <th>Черга</th>
                <th>Паралельність</th>
                <th>Канали/чати</th>
                <th>Останній успіх</th>
              </tr>
            </thead>
            <tbody>
              {data?.accounts.map((account) => (
                <tr key={account.id}>
                  <td>{account.id}</td>
                  <td>{account.label}</td>
                  <td>{account.utilization}%</td>
                  <td>{account.queued_jobs}</td>
                  <td>
                    {account.parallel_jobs}/{account.backfill_parallel_jobs}
                  </td>
                  <td>{account.dialogs_count}</td>
                  <td>{account.last_success_text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>

        <article className="card">
          <h3>Активні та проблемні задачі</h3>
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Ціль</th>
                <th>Тип задачі</th>
                <th>Акаунт</th>
                <th>Статус</th>
                <th>Що очікує</th>
                <th>Спроба</th>
                <th>Помилка</th>
              </tr>
            </thead>
            <tbody>
              {activeJobs.map((job) => (
                <tr key={job.id}>
                  <td>{job.id}</td>
                  <td>{job.target_name}</td>
                  <td>{job.job_type}</td>
                  <td>{job.account_label}</td>
                  <td>{job.status}</td>
                  <td>{job.wait_reason}</td>
                  <td>{job.attempt}</td>
                  <td>{job.error ?? "-"}</td>
                </tr>
              ))}
              {activeJobs.length === 0 ? (
                <tr>
                  <td colSpan={8}>Активних або проблемних задач немає.</td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </article>
      </section>
    </>
  );
}
