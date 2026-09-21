import { ClipboardEvent, FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otpDigits, setOtpDigits] = useState<string[]>(Array(6).fill(""));
  const [showOtpModal, setShowOtpModal] = useState(false);
  const [otpError, setOtpError] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [otpLoading, setOtpLoading] = useState(false);
  const otpRefs = useRef<Array<HTMLInputElement | null>>([]);
  const nextPath = (location.state as { from?: string } | null)?.from ?? "/";

  useEffect(() => {
    if (!showOtpModal) {
      return;
    }
    const timer = window.setTimeout(() => {
      otpRefs.current[0]?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [showOtpModal]);

  if (user) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setOtpError("");
    setLoading(true);
    try {
      await login(username, password, "");
      navigate(nextPath);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401 && err.message.includes("2FA код")) {
          setShowOtpModal(true);
          setOtpDigits(Array(6).fill(""));
          return;
        }
        if (err.status === 403 && err.message.includes("налаштування 2FA")) {
          setError("2FA не налаштовано для цього користувача. Зверніться до адміністратора.");
          return;
        }
        setError(err.message);
      } else {
        setError("Помилка авторизації");
      }
    } finally {
      setLoading(false);
    }
  };

  const otpCode = otpDigits.join("");

  const handleOtpDigitChange = (index: number, rawValue: string) => {
    const value = rawValue.replace(/\D/g, "");
    if (!value) {
      setOtpDigits((prev) => {
        const next = [...prev];
        next[index] = "";
        return next;
      });
      return;
    }

    if (value.length > 1) {
      const incoming = value.slice(0, 6).split("");
      setOtpDigits(() => Array.from({ length: 6 }, (_, i) => incoming[i] ?? ""));
      otpRefs.current[Math.min(incoming.length, 5)]?.focus();
      return;
    }

    setOtpDigits((prev) => {
      const next = [...prev];
      next[index] = value;
      return next;
    });
    if (index < 5) {
      otpRefs.current[index + 1]?.focus();
    }
  };

  const handleOtpKeyDown = (index: number, event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Backspace" && !otpDigits[index] && index > 0) {
      otpRefs.current[index - 1]?.focus();
    }
    if (event.key === "ArrowLeft" && index > 0) {
      event.preventDefault();
      otpRefs.current[index - 1]?.focus();
    }
    if (event.key === "ArrowRight" && index < 5) {
      event.preventDefault();
      otpRefs.current[index + 1]?.focus();
    }
  };

  const handleOtpPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    event.preventDefault();
    const text = event.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6);
    if (!text) {
      return;
    }
    const incoming = text.split("");
    setOtpDigits(Array.from({ length: 6 }, (_, i) => incoming[i] ?? ""));
    otpRefs.current[Math.min(incoming.length, 5)]?.focus();
  };

  const handleOtpSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setOtpError("");
    if (otpCode.length !== 6) {
      setOtpError("Введіть 6 цифр коду.");
      return;
    }

    setOtpLoading(true);
    try {
      await login(username, password, otpCode);
      navigate(nextPath);
    } catch (err) {
      if (err instanceof ApiError) {
        setOtpError(err.message);
      } else {
        setOtpError("Помилка перевірки 2FA");
      }
    } finally {
      setOtpLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>Вхід</CardTitle>
          <CardDescription>Увійдіть за логіном і паролем.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="login-username">Логін</Label>
              <Input id="login-username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </div>
            <div className="space-y-2">
              <Label htmlFor="login-password">Пароль</Label>
              <Input id="login-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
            <Button className="w-full" type="submit" disabled={loading || otpLoading}>
              {loading ? "Вхід..." : "Увійти"}
            </Button>
          </form>
          <div className="mt-4 text-center text-sm">
            <Link to="/register" className="text-primary underline underline-offset-4">
              Реєстрація за токеном
            </Link>
          </div>
        </CardContent>
      </Card>

      <Dialog open={showOtpModal} onOpenChange={(open) => !otpLoading && setShowOtpModal(open)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Підтвердження 2FA</DialogTitle>
            <DialogDescription>Введіть 6-значний код із застосунку автентифікатора.</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleOtpSubmit} className="space-y-4">
            <div className="grid grid-cols-6 gap-2">
              {otpDigits.map((digit, index) => (
                <Input
                  key={index}
                  ref={(node) => {
                    otpRefs.current[index] = node;
                  }}
                  className="h-12 text-center text-lg font-semibold"
                  value={digit}
                  onChange={(e) => handleOtpDigitChange(index, e.target.value)}
                  onKeyDown={(e) => handleOtpKeyDown(index, e)}
                  onPaste={handleOtpPaste}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={1}
                  required
                />
              ))}
            </div>
            {otpError ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{otpError}</div> : null}
            <div className="flex items-center justify-center gap-2">
              <Button type="submit" disabled={otpLoading}>
                {otpLoading ? "Перевірка..." : "Підтвердити"}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setShowOtpModal(false)} disabled={otpLoading}>
                Скасувати
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
