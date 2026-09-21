import { FormEvent, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError, confirm2FASetup, start2FASetup } from "../api";
import { useAuth } from "../auth";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";

export function Setup2FAPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const initial = (location.state as { username?: string; password?: string } | null) ?? null;
  const [username, setUsername] = useState(initial?.username ?? "");
  const [password, setPassword] = useState(initial?.password ?? "");
  const [otpCode, setOtpCode] = useState("");
  const [otpSecret, setOtpSecret] = useState("");
  const [otpUri, setOtpUri] = useState("");
  const [step, setStep] = useState<"start" | "confirm">("start");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  if (user?.totp_confirmed) {
    return <Navigate to="/" replace />;
  }

  const handleStart = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await start2FASetup(username, password);
      setOtpSecret(result.otp_secret);
      setOtpUri(result.otp_uri);
      setStep("confirm");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося запустити налаштування 2FA");
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
          <CardTitle>Налаштування 2FA</CardTitle>
          <CardDescription>Потрібно один раз підтвердити 2FA для входу.</CardDescription>
        </CardHeader>
        <CardContent>
          {step === "start" ? (
            <form onSubmit={handleStart} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="setup2fa-username">Логін</Label>
                <Input id="setup2fa-username" value={username} onChange={(e) => setUsername(e.target.value)} required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="setup2fa-password">Пароль</Label>
                <Input id="setup2fa-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
              </div>
              {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
              <Button className="w-full" type="submit" disabled={loading}>
                {loading ? "Підготовка..." : "Отримати 2FA секрет"}
              </Button>
            </form>
          ) : (
            <form onSubmit={handleConfirm} className="space-y-4">
              <div className="rounded-md border bg-muted p-4 text-sm">
                <p className="font-medium">Додайте секрет у застосунок 2FA.</p>
                <p className="mt-2">
                  <span className="font-medium">Секрет:</span> {otpSecret}
                </p>
                <p className="mt-1 break-all text-xs text-muted-foreground">URI: {otpUri}</p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="setup2fa-code">2FA код (6 цифр)</Label>
                <Input id="setup2fa-code" value={otpCode} onChange={(e) => setOtpCode(e.target.value)} inputMode="numeric" required />
              </div>
              {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
              <Button className="w-full" type="submit" disabled={loading}>
                {loading ? "Підтвердження..." : "Підтвердити і увійти"}
              </Button>
            </form>
          )}

          <div className="mt-4 flex items-center justify-center gap-4 text-sm">
            <Link to="/login" className="text-primary underline underline-offset-4">
              До входу
            </Link>
            <Link to="/register" className="text-primary underline underline-offset-4">
              Реєстрація
            </Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
