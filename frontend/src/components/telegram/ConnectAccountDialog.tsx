import { useEffect, useState } from "react";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../ui/tabs";
import { ApiError } from "../../api";
import {
  cancelTelegramQrLogin,
  completeTelegramAccountAuth,
  completeTelegramAccountReauth,
  fetchTelegramAccountReauthQrStatus,
  startTelegramAccountAuth,
  startTelegramAccountReauth,
  startTelegramAccountReauthQr,
  submitTelegramQrLoginPassword,
  type TelegramAccountRow,
  type TelegramAuthPayload,
} from "../../api/telegram";

const QR_POLL_MS = 2000;

export function ConnectAccountDialog({
  open,
  onOpenChange,
  onConnected,
  mode = "connect",
  account,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConnected: () => void;
  /** "reauth" revives a dead account's session in place instead of creating a new one. */
  mode?: "connect" | "reauth";
  account?: TelegramAccountRow | null;
}) {
  const isReauth = mode === "reauth" && !!account;

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

  const [reauthTab, setReauthTab] = useState<"phone" | "qr">("phone");
  const [qrToken, setQrToken] = useState<string | null>(null);
  const [qrSvg, setQrSvg] = useState<string | null>(null);
  const [qrStatus, setQrStatus] = useState<string | null>(null);
  const [qrPassword, setQrPassword] = useState("");
  const [qrLoading, setQrLoading] = useState(false);
  const [qrError, setQrError] = useState("");

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
    setReauthTab("phone");
    setQrToken(null);
    setQrSvg(null);
    setQrStatus(null);
    setQrPassword("");
    setQrError("");
  };

  const close = (next: boolean) => {
    if (!next) {
      if (qrToken && (qrStatus === "pending" || qrStatus === "password_needed")) {
        void cancelTelegramQrLogin(qrToken);
      }
      reset();
    }
    onOpenChange(next);
  };

  // Poll the QR session while it's still in flight (mirrors WhatsappPage's QR polling pattern).
  useEffect(() => {
    if (!isReauth || !account || !qrToken) return;
    if (qrStatus === "done" || qrStatus === "error" || qrStatus === "expired") return;
    const timer = setInterval(async () => {
      try {
        const view = await fetchTelegramAccountReauthQrStatus(account.id, qrToken);
        setQrStatus(view.status);
        setQrSvg(view.qr_svg);
        if (view.status === "error") setQrError(view.error || "Помилка QR-входу");
        if (view.status === "done") {
          reset();
          onConnected();
          onOpenChange(false);
        }
      } catch (err) {
        setQrError(err instanceof ApiError ? err.message : "Не вдалося перевірити статус QR-входу");
      }
    }, QR_POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReauth, account, qrToken, qrStatus]);

  const startQr = async () => {
    if (!account) return;
    setQrLoading(true);
    setQrError("");
    try {
      const view = await startTelegramAccountReauthQr(account.id);
      setQrToken(view.token);
      setQrSvg(view.qr_svg);
      setQrStatus(view.status);
    } catch (err) {
      setQrError(err instanceof ApiError ? err.message : "Не вдалося почати QR-вхід");
    } finally {
      setQrLoading(false);
    }
  };

  const submitQrPassword = async () => {
    if (!qrToken || !qrPassword.trim()) return;
    setQrLoading(true);
    try {
      await submitTelegramQrLoginPassword(qrToken, qrPassword.trim());
      setQrPassword("");
    } catch (err) {
      setQrError(err instanceof ApiError ? err.message : "Не вдалося надіслати пароль");
    } finally {
      setQrLoading(false);
    }
  };

  const startAuth = async () => {
    if (!isReauth && (!apiId.trim() || !apiHash.trim() || !phone.trim())) {
      setError("Для підключення акаунта заповни api_id, api_hash і phone");
      return;
    }
    setAuthLoading(true);
    setError("");
    try {
      const response =
        isReauth && account
          ? await startTelegramAccountReauth(account.id, {
              api_id: apiId.trim() || undefined,
              api_hash: apiHash.trim() || undefined,
              phone: phone.trim() || undefined,
            })
          : await startTelegramAccountAuth({ api_id: apiId.trim(), api_hash: apiHash.trim(), phone: phone.trim() });
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
    if (!isReauth && !label.trim()) {
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
      const payload = {
        api_id: pendingAuthPayload.api_id,
        api_hash: pendingAuthPayload.api_hash,
        phone: pendingAuthPayload.phone,
        temp_session_string: pendingAuthPayload.temp_session_string,
        phone_code_hash: pendingAuthPayload.phone_code_hash,
        code: code.trim(),
        password: password.trim(),
      };
      if (isReauth && account) {
        await completeTelegramAccountReauth(account.id, payload);
      } else {
        await completeTelegramAccountAuth({ ...payload, label: label.trim(), hourly_limit: Number(hourlyLimit) || 120 });
      }
      reset();
      onConnected();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не вдалося завершити авторизацію Telegram");
    } finally {
      setAuthLoading(false);
    }
  };

  const phoneFlow = (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        {!isReauth && (
          <div className="space-y-1">
            <Label htmlFor="acc-label">Мітка акаунта</Label>
            <Input id="acc-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="my_tg_1" />
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="acc-api-id">API ID</Label>
          <Input
            id="acc-api-id"
            value={apiId}
            onChange={(e) => setApiId(e.target.value)}
            placeholder={isReauth ? "з акаунта" : "1234567"}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="acc-api-hash">API Hash</Label>
          <Input
            id="acc-api-hash"
            value={apiHash}
            onChange={(e) => setApiHash(e.target.value)}
            placeholder={isReauth ? "з акаунта" : "xxxxxxxx"}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="acc-phone">Телефон</Label>
          <Input
            id="acc-phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder={isReauth ? "з акаунта" : "+380..."}
          />
        </div>
        {!isReauth && (
          <div className="space-y-1">
            <Label htmlFor="acc-hourly">Ліміт/год</Label>
            <Input id="acc-hourly" type="number" min={1} value={hourlyLimit} onChange={(e) => setHourlyLimit(e.target.value)} />
          </div>
        )}
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
              {authLoading ? "Підключаю..." : isReauth ? "Реавторизувати" : "Підключити акаунт"}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );

  const qrFlow = (
    <div className="flex flex-col items-center gap-3 py-3">
      {!qrToken ? (
        <Button type="button" onClick={() => void startQr()} disabled={qrLoading}>
          {qrLoading ? "Готую QR..." : "Показати QR-код"}
        </Button>
      ) : (
        <>
          {qrSvg && (qrStatus === "pending" || qrStatus === "password_needed") ? (
            <img src={qrSvg} alt="QR-код для входу в Telegram" className="h-56 w-56 rounded-md border bg-white" />
          ) : null}
          {qrStatus === "pending" && (
            <p className="text-center text-sm text-muted-foreground">
              У Telegram: Налаштування → Пристрої → Прив'язати пристрій, і відскануй код.
            </p>
          )}
          {qrStatus === "password_needed" && (
            <div className="w-full space-y-2">
              <Label htmlFor="qr-pass">Пароль 2FA Telegram</Label>
              <Input id="qr-pass" type="password" value={qrPassword} onChange={(e) => setQrPassword(e.target.value)} />
              <Button type="button" className="w-full" onClick={() => void submitQrPassword()} disabled={qrLoading || !qrPassword.trim()}>
                Підтвердити пароль
              </Button>
            </div>
          )}
          {qrStatus === "expired" && (
            <p className="text-sm text-destructive">
              QR-код прострочено.{" "}
              <button type="button" className="underline" onClick={() => void startQr()}>
                Отримати новий
              </button>
            </p>
          )}
          {qrError && <p className="text-sm text-destructive">{qrError}</p>}
        </>
      )}
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{isReauth ? `Реавторизувати «${account?.label}»` : "Підключити Telegram-акаунт"}</DialogTitle>
          <DialogDescription>
            {isReauth
              ? "Оживити сесію того самого акаунта — кодом із Telegram або QR."
              : "Авторизація через код Telegram (без session string)."}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}

          {isReauth && (
            <div className="space-y-1 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
              <p>api_id і api_hash беруться зі збереженого акаунта — вводити їх знову не треба.</p>
              <p>
                Це лише оживить сесію. Канали, які фейловер вже відв&apos;язав від цього акаунта, автоматично не повернуться —
                признач їх заново на сторінці акаунта.
              </p>
            </div>
          )}

          {isReauth ? (
            <Tabs value={reauthTab} onValueChange={(v) => setReauthTab(v as "phone" | "qr")}>
              <TabsList>
                <TabsTrigger value="phone">Код Telegram</TabsTrigger>
                <TabsTrigger value="qr">QR-код</TabsTrigger>
              </TabsList>
              <TabsContent value="phone">{phoneFlow}</TabsContent>
              <TabsContent value="qr">{qrFlow}</TabsContent>
            </Tabs>
          ) : (
            phoneFlow
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
