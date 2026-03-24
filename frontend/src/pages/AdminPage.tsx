import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  type AdminRegistrationToken,
  type AdminResources,
  type AdminUserRow,
  ApiError,
  adminCreateRegistrationToken,
  adminDeactivateRegistrationToken,
  adminGetResources,
  adminListRegistrationTokens,
  adminListUsers,
  adminUpdateUser
} from "../api";

export function AdminPage() {
  const [tokens, setTokens] = useState<AdminRegistrationToken[]>([]);
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [resources, setResources] = useState<AdminResources | null>(null);
  const [label, setLabel] = useState("");
  const [maxUses, setMaxUses] = useState(1);
  const [expiresInHours, setExpiresInHours] = useState(24);
  const [issuedToken, setIssuedToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [tokenRows, userRows, resourceRows] = await Promise.all([
        adminListRegistrationTokens(),
        adminListUsers(),
        adminGetResources()
      ]);
      setTokens(tokenRows);
      setUsers(userRows);
      setResources(resourceRows);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити адмін-панель");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleCreateToken = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const result = await adminCreateRegistrationToken(label, maxUses, expiresInHours);
      setIssuedToken(result.token);
      setLabel("");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити токен");
      }
    }
  };

  const handleDeactivateToken = async (tokenId: number) => {
    setError("");
    try {
      await adminDeactivateRegistrationToken(tokenId);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося деактивувати токен");
      }
    }
  };

  const handleUserRoleChange = async (userId: number, role: "admin" | "user") => {
    setError("");
    try {
      await adminUpdateUser(userId, { role });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося оновити роль");
      }
    }
  };

  const handleUserActiveToggle = async (row: AdminUserRow) => {
    setError("");
    try {
      await adminUpdateUser(row.id, { is_active: !row.is_active });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося змінити статус користувача");
      }
    }
  };

  return (
    <>
      <section className="card module-hero">
        <h2>Адмін-панель</h2>
        <p className="hint">Керування інвайт-токенами, ролями та власниками ресурсів.</p>
        {error ? <div className="error-box">{error}</div> : null}
      </section>

      <section className="card">
        <h3>Токени реєстрації</h3>
        <form onSubmit={handleCreateToken} className="inline">
          <input placeholder="Мітка (опційно)" value={label} onChange={(e) => setLabel(e.target.value)} />
          <input
            type="number"
            min={1}
            max={1000}
            value={maxUses}
            onChange={(e) => setMaxUses(Math.max(1, Number(e.target.value) || 1))}
          />
          <input
            type="number"
            min={0}
            max={720}
            value={expiresInHours}
            onChange={(e) => setExpiresInHours(Math.max(0, Number(e.target.value) || 0))}
          />
          <button type="submit">Згенерувати токен</button>
        </form>
        <p className="hint">Поля: мітка, max_uses, expires_in_hours (0 = без строку дії).</p>
        {issuedToken ? (
          <div className="info-box">
            <div>
              <strong>Новий токен:</strong> {issuedToken}
            </div>
            <div className="hint">Збережіть його зараз: після закриття вікна показується тільки hash.</div>
          </div>
        ) : null}

        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Мітка</th>
              <th>Стан</th>
              <th>Використано</th>
              <th>Ким створено</th>
              <th>Ким використано</th>
              <th>Строк дії</th>
              <th>Дія</th>
            </tr>
          </thead>
          <tbody>
            {tokens.map((token) => (
              <tr key={token.id}>
                <td>{token.id}</td>
                <td>{token.label ?? "-"}</td>
                <td>{token.is_active ? (token.is_expired ? "Прострочений" : "Активний") : "Вимкнений"}</td>
                <td>
                  {token.used_count}/{token.max_uses}
                </td>
                <td>{token.created_by_username ?? token.created_by_user_id ?? "-"}</td>
                <td>{token.used_by_username ?? token.used_by_user_id ?? "-"}</td>
                <td>{token.expires_at ?? "-"}</td>
                <td>
                  {token.is_active ? (
                    <button className="danger" type="button" onClick={() => void handleDeactivateToken(token.id)}>
                      Вимкнути
                    </button>
                  ) : (
                    "-"
                  )}
                </td>
              </tr>
            ))}
            {!loading && tokens.length === 0 ? (
              <tr>
                <td colSpan={8}>Токенів немає.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h3>Користувачі</h3>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Логін</th>
              <th>Роль</th>
              <th>Стан</th>
              <th>2FA</th>
              <th>Токен</th>
            </tr>
          </thead>
          <tbody>
            {users.map((row) => (
              <tr key={row.id}>
                <td>{row.id}</td>
                <td>{row.username}</td>
                <td>
                  <select value={row.role} onChange={(e) => void handleUserRoleChange(row.id, e.target.value as "admin" | "user")}>
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                </td>
                <td>
                  <button type="button" className={row.is_active ? "" : "danger"} onClick={() => void handleUserActiveToggle(row)}>
                    {row.is_active ? "Активний" : "Вимкнений"}
                  </button>
                </td>
                <td>{row.totp_confirmed ? "Підтверджено" : "Не підтверджено"}</td>
                <td>{row.created_by_token_id ?? "-"}</td>
              </tr>
            ))}
            {!loading && users.length === 0 ? (
              <tr>
                <td colSpan={6}>Користувачів немає.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h3>Ресурси та власники</h3>
        <div className="grid-2">
          <article>
            <h4>Акаунти парсингу</h4>
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Мітка</th>
                  <th>Власник</th>
                </tr>
              </thead>
              <tbody>
                {resources?.accounts.map((item) => (
                  <tr key={item.id}>
                    <td>{item.id}</td>
                    <td>{item.label}</td>
                    <td>{item.owner_username ?? item.owner_user_id ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </article>
          <article>
            <h4>Цілі парсингу</h4>
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Назва</th>
                  <th>Власник</th>
                </tr>
              </thead>
              <tbody>
                {resources?.targets.map((item) => (
                  <tr key={item.id}>
                    <td>{item.id}</td>
                    <td>{item.name}</td>
                    <td>{item.owner_username ?? item.owner_user_id ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </article>
        </div>
        <h4>Привʼязки ціль ↔ акаунт</h4>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Ціль</th>
              <th>Акаунт</th>
              <th>Власник</th>
              <th>Стан</th>
            </tr>
          </thead>
          <tbody>
            {resources?.links.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.target_id}</td>
                <td>{item.account_id}</td>
                <td>{item.owner_username ?? item.owner_user_id ?? "-"}</td>
                <td>{item.is_active ? "Активна" : "Вимкнена"}</td>
              </tr>
            ))}
            {!loading && (!resources || resources.links.length === 0) ? (
              <tr>
                <td colSpan={5}>Привʼязок немає.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>
    </>
  );
}
