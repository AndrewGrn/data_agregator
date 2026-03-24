import { FormEvent, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { ApiError, confirm2FASetup, registerWithInvite } from "../api";
import { useAuth } from "../auth";

export function RegisterPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const [inviteToken, setInviteToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otpCode, setOtpCode] = useState("");
  const [otpSecret, setOtpSecret] = useState("");
  const [otpUri, setOtpUri] = useState("");
  const [step, setStep] = useState<"register" | "confirm">("register");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  if (user) {
    return <Navigate to="/" replace />;
  }

  const handleRegister = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await registerWithInvite(inviteToken, username, password);
      setOtpSecret(result.otp_secret);
      setOtpUri(result.otp_uri);
      setStep("confirm");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Помилка реєстрації");
      }
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      await confirm2FASetup(username, password, otpCode);
      await login(username, password, otpCode);
      navigate("/");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося підтвердити 2FA");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="centered">
      <section className="card login-card auth-card-wide">
        <h2>Реєстрація</h2>
        <p className="hint">Створення користувача за токеном, який згенерував адміністратор.</p>

        {step === "register" ? (
          <form onSubmit={handleRegister}>
            <label>Токен реєстрації</label>
            <input value={inviteToken} onChange={(e) => setInviteToken(e.target.value)} required />
            <label>Логін</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} required />
            <label>Пароль</label>
            <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" required />
            {error ? <div className="error-box">{error}</div> : null}
            <button type="submit" disabled={loading}>
              {loading ? "Створення..." : "Створити акаунт"}
            </button>
          </form>
        ) : (
          <form onSubmit={handleConfirm}>
            <div className="info-box">
              <div>Додайте секрет у застосунок 2FA (Google Authenticator / Authy / 1Password).</div>
              <div>
                <strong>Секрет:</strong> {otpSecret}
              </div>
              <div className="hint">URI: {otpUri}</div>
            </div>
            <label>2FA код (6 цифр)</label>
            <input value={otpCode} onChange={(e) => setOtpCode(e.target.value)} inputMode="numeric" required />
            {error ? <div className="error-box">{error}</div> : null}
            <button type="submit" disabled={loading}>
              {loading ? "Підтвердження..." : "Підтвердити 2FA і увійти"}
            </button>
          </form>
        )}

        <div className="inline auth-links">
          <Link to="/login">До входу</Link>
          <Link to="/setup-2fa">Є акаунт, але 2FA не налаштовано</Link>
        </div>
      </section>
    </div>
  );
}
