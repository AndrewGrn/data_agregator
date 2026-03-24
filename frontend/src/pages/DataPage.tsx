import { useEffect, useMemo, useState } from "react";
import { ApiError, apiGet } from "../api";

type TargetOverview = {
  id: number;
  name: string;
  identifier: string;
  events_count: number;
  last_event_text: string;
};

type MessageRow = {
  id: number;
  external_id: string | null;
  message_kind: string;
  is_comment: boolean;
  sender_label: string;
  sender_id: number | null;
  text: string;
  root_post_id: number | null;
  parent_message_id: number | null;
  observed_at_text: string;
};

type UserRow = {
  telegram_user_id: number;
  display_name: string;
  full_name: string | null;
  memberships_count: number;
  status_text: string;
  is_active_any: boolean;
  last_seen_text: string;
};

export function DataPage() {
  const [targets, setTargets] = useState<TargetOverview[]>([]);
  const [selectedTargetId, setSelectedTargetId] = useState<number | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [search, setSearch] = useState("");
  const [chatLimit, setChatLimit] = useState(200);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    apiGet<TargetOverview[]>("/api/telegram/targets-overview")
      .then((rows) => {
        if (!active) {
          return;
        }
        setTargets(rows);
        if (rows.length > 0) {
          setSelectedTargetId((prev) => prev ?? rows[0].id);
        }
      })
      .catch((err) => {
        if (!active) {
          return;
        }
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Не вдалося завантажити цілі");
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

  useEffect(() => {
    if (!selectedTargetId) {
      setMessages([]);
      setUsers([]);
      return;
    }

    let active = true;
    setError("");

    const load = async () => {
      try {
        const [messagesResponse, usersResponse] = await Promise.all([
          apiGet<{ messages: MessageRow[] }>(
            `/api/telegram/targets/${selectedTargetId}/messages?limit=${chatLimit}&q=${encodeURIComponent(search)}`
          ),
          apiGet<{ users: UserRow[] }>(`/api/telegram/targets/${selectedTargetId}/users?limit=500`)
        ]);
        if (!active) {
          return;
        }
        setMessages(messagesResponse.messages);
        setUsers(usersResponse.users);
      } catch (err) {
        if (!active) {
          return;
        }
        if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Не вдалося завантажити дані цілі");
        }
      }
    };

    void load();
    const timer = setInterval(() => {
      void load();
    }, 12000);

    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [selectedTargetId, search, chatLimit]);

  const selectedTarget = useMemo(
    () => targets.find((target) => target.id === selectedTargetId) ?? null,
    [targets, selectedTargetId]
  );

  return (
    <>
      <section className="card module-hero">
        <h2>Telegram дані (React)</h2>
        <p className="hint">Список чатів/каналів, стрічка повідомлень та користувачі.</p>
        {error ? <div className="error-box">{error}</div> : null}
        <div className="inline">
          <label>Пошук по повідомленнях</label>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Текст, sender, id" />
          <label>Ліміт чату</label>
          <input
            value={chatLimit}
            type="number"
            min={50}
            max={1000}
            onChange={(e) => setChatLimit(Math.max(50, Math.min(1000, Number(e.target.value) || 200)))}
          />
        </div>
      </section>

      <section className="card">
        <div className="tg-data-layout">
          <aside className="tg-targets-panel">
            <h4>Канали і чати</h4>
            <div className="tg-targets-list">
              {targets.map((target) => (
                <button
                  key={target.id}
                  type="button"
                  className={`tg-target-item ${selectedTargetId === target.id ? "selected" : ""}`}
                  onClick={() => setSelectedTargetId(target.id)}
                >
                  <div className="tg-target-title">{target.name}</div>
                  <div className="hint">{target.identifier}</div>
                  <div className="tg-target-meta">
                    <span>Подій: {target.events_count}</span>
                    <span>Остання: {target.last_event_text}</span>
                  </div>
                </button>
              ))}
              {!loading && targets.length === 0 ? <div className="hint">Цілей поки немає.</div> : null}
            </div>
          </aside>

          <section className="tg-chat-panel">
            <h4>
              {selectedTarget ? `Стрічка: ${selectedTarget.name} (${selectedTarget.identifier})` : "Стрічка повідомлень"}
            </h4>
            <div className="tg-chat-stream">
              {messages.map((msg) => (
                <article key={msg.id} className={`tg-msg ${msg.is_comment ? "comment" : ""}`}>
                  <div className="tg-msg-head">
                    <strong>{msg.sender_label}</strong>
                    <span>{msg.observed_at_text}</span>
                  </div>
                  <p>{msg.text}</p>
                  <div className="tg-msg-meta">
                    <span>{msg.message_kind}</span>
                    <span>ID події: {msg.id}</span>
                    <span>External: {msg.external_id ?? "-"}</span>
                    {msg.is_comment ? <span>Root: {msg.root_post_id ?? "-"} | Reply: {msg.parent_message_id ?? "-"}</span> : null}
                  </div>
                </article>
              ))}
              {messages.length === 0 ? <div className="hint">Повідомлень не знайдено.</div> : null}
            </div>
          </section>

          <aside className="tg-users-panel">
            <h4>Користувачі</h4>
            <div className="tg-users-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Користувач</th>
                    <th>Стан</th>
                    <th>Остання активність</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr key={user.telegram_user_id}>
                      <td>
                        <div>{user.display_name}</div>
                        <div className="hint">{user.full_name ?? "-"}</div>
                        <div className="hint">ID: {user.telegram_user_id}</div>
                      </td>
                      <td>
                        <div>{user.status_text}</div>
                        <div className="hint">{user.is_active_any ? "Активний" : "Неактивний"}</div>
                        <div className="hint">Записів: {user.memberships_count}</div>
                      </td>
                      <td>{user.last_seen_text}</td>
                    </tr>
                  ))}
                  {users.length === 0 ? (
                    <tr>
                      <td colSpan={3}>Користувачів не знайдено.</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </aside>
        </div>
      </section>
    </>
  );
}
