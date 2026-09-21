import { useEffect, useRef, useState } from "react";
import { ApiError, createWhatsappAccount, fetchWhatsappStatus, WhatsappAccountStatus } from "../api";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";

const POLL_INTERVAL_MS = 2000;

export function WhatsappPage() {
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [createMessage, setCreateMessage] = useState("");
  const [status, setStatus] = useState<WhatsappAccountStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
      }
    };
  }, []);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const pollStatus = (id: number) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const next = await fetchWhatsappStatus(id);
        setStatus(next);
        if (next.status === "ready") {
          stopPolling();
        }
      } catch (err) {
        // transient — keep polling, the next tick may succeed
      }
    }, POLL_INTERVAL_MS);
  };

  const handleCreate = async () => {
    if (!label.trim()) {
      setError("Вкажи мітку акаунта");
      return;
    }
    setCreating(true);
    setError("");
    try {
      const created = await createWhatsappAccount(label.trim());
      setAccountId(created.id);
      setCreateMessage(created.message);
      setStatus({ status: created.status, qr_data_url: null, updated_at: null });
      pollStatus(created.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не вдалося створити акаунт WhatsApp");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>WhatsApp</CardTitle>
          <CardDescription>Прив'язка нового акаунта WhatsApp через QR-код.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}

          {!accountId ? (
            <div className="flex flex-col gap-2 sm:max-w-sm">
              <Label htmlFor="wa-label">Мітка акаунта</Label>
              <Input id="wa-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="напр. wa-main" />
              <Button type="button" onClick={handleCreate} disabled={creating}>
                {creating ? "Створення..." : "Створити акаунт"}
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              {createMessage ? (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">{createMessage}</div>
              ) : null}

              {status?.status === "ready" ? (
                <div className="rounded-md border border-emerald-300 bg-emerald-50 p-3 text-sm text-emerald-900">
                  Акаунт успішно прив'язано. WhatsApp готовий приймати повідомлення.
                </div>
              ) : status?.status === "qr" && status.qr_data_url ? (
                <div className="flex flex-col items-center gap-2">
                  <img src={status.qr_data_url} alt="WhatsApp QR" className="h-64 w-64 rounded-md border" />
                  <p className="text-sm text-muted-foreground">
                    Відскануйте код у WhatsApp → Зв'язані пристрої
                  </p>
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">
                  Очікування QR-коду від мосту (статус: {status?.status ?? "pending"})...
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
