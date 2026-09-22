import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Link2, TriangleAlert } from "lucide-react";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { EmptyState } from "../../components/ui/empty-state";
import { StatusDot } from "../../components/ui/status-dot";
import { StatusPill } from "../../components/ui/status-pill";
import { TargetRow } from "../../components/telegram/TargetRow";
import { ConnectAccountDialog } from "../../components/telegram/ConnectAccountDialog";
import {
  fetchTelegramAccounts,
  fetchTelegramAccountTargets,
  onboardTelegramTarget,
  type TelegramAccountRow,
  type TelegramTargetRow,
} from "../../api/telegram";

export function AccountPage() {
  const { id } = useParams();
  const accountId = Number(id);
  const [account, setAccount] = useState<TelegramAccountRow | null>(null);
  const [targets, setTargets] = useState<TelegramTargetRow[]>([]);
  const [assign, setAssign] = useState("");
  const [join, setJoin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reauthOpen, setReauthOpen] = useState(false);

  const load = useCallback(async () => {
    const [accounts, rows] = await Promise.all([fetchTelegramAccounts(), fetchTelegramAccountTargets(accountId)]);
    setAccount(accounts.find((a) => a.id === accountId) ?? null);
    setTargets(rows);
  }, [accountId]);

  useEffect(() => {
    void load();
  }, [load]);

  const submitAssign = async () => {
    if (!assign.trim()) return;
    setError(null);
    try {
      await onboardTelegramTarget(assign.trim(), { account_id: accountId, allow_join: join });
      setAssign("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Помилка");
    }
  };

  if (!account) return null;
  const tone = account.alive === false ? "bad" : account.alive ? "ok" : "muted";

  return (
    <div className="flex flex-col gap-5">
      <Link to="/telegram/settings" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="h-4 w-4" /> Акаунти
      </Link>

      <header className="flex flex-wrap items-center gap-3">
        <StatusDot tone={tone} />
        <h1 className="text-2xl font-semibold">{account.label}</h1>
        <StatusPill tone={account.pool_mode === "shared" ? "info" : "muted"}>
          {account.pool_mode === "shared" ? "Пул" : "Приватний"}
        </StatusPill>
        <span className="text-sm text-muted-foreground">
          {account.targets_count} каналів · вступів {account.joins_today}/{account.join_daily_limit}
        </span>
        {account.dead_reason && <span className="text-sm text-destructive">{account.dead_reason}</span>}
      </header>

      {(account.alive === false || account.dead_reason) && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-4">
          <div className="flex items-start gap-2 text-sm text-destructive">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <p className="font-medium">Сесія цього акаунта не авторизована — він нічого не збирає.</p>
              <p className="text-destructive/80">{account.dead_reason || "Telegram відхилив сесію."}</p>
            </div>
          </div>
          <Button variant="destructive" onClick={() => setReauthOpen(true)}>
            Реавторизувати
          </Button>
        </div>
      )}

      <ConnectAccountDialog mode="reauth" account={account} open={reauthOpen} onOpenChange={setReauthOpen} onConnected={load} />

      {account.pool_mode === "dedicated" && (
        <form className="flex flex-col gap-2 rounded-lg border bg-card p-4" onSubmit={(e) => { e.preventDefault(); void submitAssign(); }}>
          <label className="text-sm font-medium">Призначити канал цьому акаунту</label>
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Link2 className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input className="pl-9" placeholder="Посилання, @канал або ID" value={assign} onChange={(e) => setAssign(e.target.value)} />
            </div>
            <Button type="submit" disabled={!assign.trim()}>Призначити</Button>
          </div>
          <label className="inline-flex items-center gap-2 text-sm text-muted-foreground">
            <input type="checkbox" checked={join} onChange={(e) => setJoin(e.target.checked)} />
            Вступити, якщо акаунт ще не учасник
          </label>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </form>
      )}

      {targets.length === 0 ? (
        <EmptyState icon={Link2} title="Цей акаунт ще нічого не парсить" />
      ) : (
        <div className="divide-y rounded-lg border bg-card">
          {targets.map((row) => <TargetRow key={row.id} row={row} onChanged={load} />)}
        </div>
      )}
    </div>
  );
}
