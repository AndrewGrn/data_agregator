import { FormEvent, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otpCode, setOtpCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (user) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password, otpCode);
      const nextPath = (location.state as { from?: string } | null)?.from ?? "/";
      navigate(nextPath);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Помилка авторизації");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="centered">
      <section className="card login-card">
        <h2>Вхід</h2>
        <p className="hint">Вхід за логіном, паролем і 2FA-кодом.</p>
        <form onSubmit={handleSubmit}>
          <label>Логін</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} required />
          <label>Пароль</label>
          <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" required />
          <label>2FA код (6 цифр)</label>
          <input value={otpCode} onChange={(e) => setOtpCode(e.target.value)} inputMode="numeric" required />
          {error ? <div className="error-box">{error}</div> : null}
          <button type="submit" disabled={loading}>
            {loading ? "Вхід..." : "Увійти"}
          </button>
        </form>
        <div className="inline auth-links">
          <Link to="/register">Реєстрація за токеном</Link>
          <Link to="/setup-2fa">Налаштувати 2FA</Link>
        </div>
      </section>
    </div>
  );
}
