import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, MoreHorizontal, Plus, Users2 } from "lucide-react";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { Popover, PopoverContent, PopoverTrigger } from "../../components/ui/popover";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import { AccountRow } from "../../components/telegram/AccountRow";
import { ConnectAccountDialog } from "../../components/telegram/ConnectAccountDialog";
import {
  fetchTelegramAccounts,
  fetchTelegramModule,
  resetTelegramAccountsCooldown,
  retryTelegramJob,
  retryTelegramJobsFailed,
  syncTelegramMemberships,
  type TelegramAccountRow,
  type TelegramJobRow,
} from "../../api/telegram";

function AccountsTab() {
  const [accounts, setAccounts] = useState<TelegramAccountRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [connectOpen, setConnectOpen] = useState(false);
  const [reauthAccount, setReauthAccount] = useState<TelegramAccountRow | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const load = useCallback(async () => {
    const rows = await fetchTelegramAccounts();
    setAccounts(rows);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const runService = async (key: string, fn: () => Promise<unknown>) => {
    setBusyAction(key);
    try {
      await fn();
      await load();
    } finally {
      setBusyAction(null);
      setMenuOpen(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end gap-2">
        <Popover open={menuOpen} onOpenChange={setMenuOpen}>
          <PopoverTrigger asChild>
            <Button variant="outline" size="icon" aria-label="Сервісні дії">
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="flex w-64 flex-col gap-1 p-1">
            <Button
              variant="ghost"
              className="justify-start"
              disabled={busyAction === "sync"}
              onClick={() => void runService("sync", syncTelegramMemberships)}
            >
              Синхронізувати учасників
            </Button>
            <Button
              variant="ghost"
              className="justify-start"
              disabled={busyAction === "cooldown"}
              onClick={() => void runService("cooldown", resetTelegramAccountsCooldown)}
            >
              Скинути cooldown
            </Button>
          </PopoverContent>
        </Popover>
        <Button onClick={() => setConnectOpen(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Підключити акаунт
        </Button>
      </div>

      {loading ? null : accounts.length === 0 ? (
        <EmptyState icon={Users2} title="Ще немає підключених акаунтів" hint="Підключіть акаунт, щоб система могла вступати в канали." />
      ) : (
        <div className="divide-y rounded-lg border bg-card">
          {accounts.map((row) => (
            <AccountRow key={row.id} row={row} onChanged={load} onReauth={setReauthAccount} />
          ))}
        </div>
      )}

      <ConnectAccountDialog open={connectOpen} onOpenChange={setConnectOpen} onConnected={load} />
      {reauthAccount && (
        <ConnectAccountDialog
          mode="reauth"
          account={reauthAccount}
          open
          onOpenChange={(next) => !next && setReauthAccount(null)}
          onConnected={load}
        />
      )}
    </div>
  );
}

function JobsTab() {
  const [jobs, setJobs] = useState<TelegramJobRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [retryingJobId, setRetryingJobId] = useState<number | null>(null);
  const [retryingAll, setRetryingAll] = useState(false);

  const load = useCallback(async () => {
    const data = await fetchTelegramModule();
    setJobs(data.jobs.filter((job) => job.status !== "succeeded"));
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const retryOne = async (jobId: number) => {
    setRetryingJobId(jobId);
    try {
      await retryTelegramJob(jobId);
      await load();
    } finally {
      setRetryingJobId(null);
    }
  };

  const retryAllFailed = async () => {
    setRetryingAll(true);
    try {
      await retryTelegramJobsFailed();
      await load();
    } finally {
      setRetryingAll(false);
    }
  };

  if (loading) return null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-end">
        <Button variant="outline" size="sm" disabled={retryingAll} onClick={() => void retryAllFailed()}>
          {retryingAll ? "Перезапуск..." : "Перезапустити всі помилкові"}
        </Button>
      </div>
      {jobs.length === 0 ? (
        <EmptyState icon={Users2} title="Активних або проблемних задач немає" />
      ) : (
        <div className="overflow-x-auto rounded-lg border bg-card">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Ціль</th>
                <th className="px-3 py-2">Тип</th>
                <th className="px-3 py-2">Акаунт</th>
                <th className="px-3 py-2">Статус</th>
                <th className="px-3 py-2">Спроба</th>
                <th className="px-3 py-2">Помилка</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id} className="border-b last:border-b-0">
                  <td className="px-3 py-2">{job.target_name}</td>
                  <td className="px-3 py-2">{job.job_type}</td>
                  <td className="px-3 py-2">{job.account_label}</td>
                  <td className="px-3 py-2">{job.status}</td>
                  <td className="px-3 py-2">{job.attempt}</td>
                  <td className="max-w-xs truncate px-3 py-2 text-muted-foreground" title={job.error ?? undefined}>
                    {job.status === "failed" || job.status === "retry" ? job.error ?? "-" : "-"}
                  </td>
                  <td className="px-3 py-2">
                    {job.status === "pending" || job.status === "failed" || job.status === "retry" ? (
                      <Button size="sm" variant="outline" disabled={retryingJobId === job.id} onClick={() => void retryOne(job.id)}>
                        {retryingJobId === job.id ? "..." : job.status === "pending" ? "Запустити" : "Перезапустити"}
                      </Button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function SettingsPage() {
  // ponytail: full error-log journal (per-target filter, 300-row history) from the old
  // page is not ported — the active/problem jobs table already shows each job's error
  // inline. Add a dedicated journal view if debugging needs history beyond current jobs.
  return (
    <div className="flex flex-col gap-5">
      <Link to="/telegram" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline">
        <ArrowLeft className="h-4 w-4" /> Telegram
      </Link>
      <h1 className="text-2xl font-semibold">Налаштування Telegram</h1>

      <Tabs defaultValue="accounts">
        <TabsList>
          <TabsTrigger value="accounts">Акаунти</TabsTrigger>
          <TabsTrigger value="jobs">Задачі</TabsTrigger>
        </TabsList>
        <TabsContent value="accounts" className="mt-4">
          <AccountsTab />
        </TabsContent>
        <TabsContent value="jobs" className="mt-4">
          <JobsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
