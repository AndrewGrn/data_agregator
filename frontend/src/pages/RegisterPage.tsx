import { FormEvent, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { ApiError, confirm2FASetup, registerWithInvite } from "../api";
import { useAuth } from "../auth";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";

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
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-2xl">
        <CardHeader>
          <CardTitle>Реєстрація</CardTitle>
          <CardDescription>Створення користувача за токеном, який згенерував адміністратор.</CardDescription>
        </CardHeader>
        <CardContent>
          {step === "register" ? (
            <form onSubmit={handleRegister} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="register-token">Токен реєстрації</Label>
                <Input id="register-token" value={inviteToken} onChange={(e) => setInviteToken(e.target.value)} required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="register-username">Логін</Label>
                <Input id="register-username" value={username} onChange={(e) => setUsername(e.target.value)} required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="register-password">Пароль</Label>
                <Input id="register-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              </div>
              {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
              <Button className="w-full" type="submit" disabled={loading}>
                {loading ? "Створення..." : "Створити акаунт"}
              </Button>
            </form>
          ) : (
            <form onSubmit={handleConfirm} className="space-y-4">
              <div className="rounded-md border bg-muted p-4 text-sm">
                <p className="font-medium">Додайте секрет у застосунок 2FA (Google Authenticator / Authy / 1Password).</p>
                <p className="mt-2">
                  <span className="font-medium">Секрет:</span> {otpSecret}
                </p>
                <p className="mt-1 break-all text-xs text-muted-foreground">URI: {otpUri}</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="register-otp">2FA код (6 цифр)</Label>
                <Input id="register-otp" value={otpCode} onChange={(e) => setOtpCode(e.target.value)} inputMode="numeric" required />
              </div>
              {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
              <Button className="w-full" type="submit" disabled={loading}>
                {loading ? "Підтвердження..." : "Підтвердити 2FA і увійти"}
              </Button>
            </form>
          )}

          <div className="mt-4 text-center text-sm">
            <Link to="/login" className="text-primary underline underline-offset-4">
              До входу
            </Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
