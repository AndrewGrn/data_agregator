import { useState } from "react";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { ApiError } from "../../api";
import { completeTelegramAccountAuth, startTelegramAccountAuth, type TelegramAuthPayload } from "../../api/telegram";

export function ConnectAccountDialog({
  open,
  onOpenChange,
  onConnected,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConnected: () => void;
}) {
  const [label, setLabel] = useState("");
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [phone, setPhone] = useState("");
  const [hourlyLimit, setHourlyLimit] = useState("120");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [authLoading, setAuthLoading] = useState(false);
  const [pendingAuthPayload, setPendingAuthPayload] = useState<TelegramAuthPayload | null>(null);
  const [error, setError] = useState("");

  const reset = () => {
    setLabel("");
    setApiId("");
    setApiHash("");
    setPhone("");
    setHourlyLimit("120");
    setCode("");
    setPassword("");
    setPendingAuthPayload(null);
    setError("");
  };

  const close = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const startAuth = async () => {
    if (!apiId.trim() || !apiHash.trim() || !phone.trim()) {
      setError("Для підключення акаунта заповни api_id, api_hash і phone");
      return;
    }
    setAuthLoading(true);
    setError("");
    try {
      const response = await startTelegramAccountAuth({ api_id: apiId.trim(), api_hash: apiHash.trim(), phone: phone.trim() });
      setPendingAuthPayload(response.auth_payload);
      setCode("");
      setPassword("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не вдалося надіслати код Telegram");
    } finally {
      setAuthLoading(false);
    }
  };

  const completeAuth = async () => {
    if (!pendingAuthPayload) {
      setError("Спочатку натисни 'Надіслати код'");
      return;
    }
    if (!label.trim()) {
      setError("Вкажи мітку акаунта");
      return;
    }
    if (!code.trim()) {
      setError("Введи код із Telegram");
      return;
    }
    setAuthLoading(true);
    setError("");
    try {
      await completeTelegramAccountAuth({
        label: label.trim(),
        hourly_limit: Number(hourlyLimit) || 120,
        api_id: pendingAuthPayload.api_id,
        api_hash: pendingAuthPayload.api_hash,
        phone: pendingAuthPayload.phone,
        temp_session_string: pendingAuthPayload.temp_session_string,
        phone_code_hash: pendingAuthPayload.phone_code_hash,
        code: code.trim(),
        password: password.trim(),
      });
      reset();
      onConnected();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не вдалося завершити авторизацію Telegram");
    } finally {
      setAuthLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Підключити Telegram-акаунт</DialogTitle>
          <DialogDescription>Авторизація через код Telegram (без session string).</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="acc-label">Мітка акаунта</Label>
              <Input id="acc-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="my_tg_1" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-api-id">API ID</Label>
              <Input id="acc-api-id" value={apiId} onChange={(e) => setApiId(e.target.value)} placeholder="1234567" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-api-hash">API Hash</Label>
              <Input id="acc-api-hash" value={apiHash} onChange={(e) => setApiHash(e.target.value)} placeholder="xxxxxxxx" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-phone">Телефон</Label>
              <Input id="acc-phone" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+380..." />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-hourly">Ліміт/год</Label>
              <Input id="acc-hourly" type="number" min={1} value={hourlyLimit} onChange={(e) => setHourlyLimit(e.target.value)} />
            </div>
          </div>

          <div className="flex items-center justify-end">
            <Button type="button" variant="outline" onClick={() => void startAuth()} disabled={authLoading}>
              {authLoading ? "Надсилаю..." : "Надіслати код"}
            </Button>
          </div>

          {pendingAuthPayload ? (
            <div className="space-y-3 rounded-md border p-3">
              <p className="text-sm text-muted-foreground">Код надіслано на {pendingAuthPayload.phone}. Введи код для завершення.</p>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="acc-code">Код із Telegram</Label>
                  <Input id="acc-code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="12345" />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="acc-pass">Пароль 2FA Telegram (якщо є)</Label>
                  <Input id="acc-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Необов'язково" />
                </div>
              </div>
              <div className="flex items-center justify-end gap-2">
                <Button type="button" variant="ghost" onClick={() => setPendingAuthPayload(null)} disabled={authLoading}>
                  Скасувати
                </Button>
                <Button type="button" onClick={() => void completeAuth()} disabled={authLoading}>
                  {authLoading ? "Підключаю..." : "Підключити акаунт"}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
