import { useState } from "react";
import { Link2, Loader2 } from "lucide-react";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { ApiError } from "../../api";
import { onboardTelegramTarget, type TelegramTargetRow } from "../../api/telegram";

export function OnboardBar({ onQueued }: { onQueued: (row: TelegramTargetRow) => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const input = value.trim();
    if (!input) return;
    setBusy(true);
    setError(null);
    try {
      const row = await onboardTelegramTarget(input);
      onQueued(row);
      setValue("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не вдалося поставити в чергу");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-1">
      <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <div className="relative flex-1">
          <Link2 className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-9"
            name="telegram-onboard-input"
            placeholder="Посилання, @канал або ID — система сама обере акаунт і вступить"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            disabled={busy}
            aria-label="Ідентифікатор каналу"
          />
        </div>
        <Button type="submit" disabled={busy || !value.trim()}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : "Підключити"}
        </Button>
      </form>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
