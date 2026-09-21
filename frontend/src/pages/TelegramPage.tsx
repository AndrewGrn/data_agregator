import { Pencil, Play, RefreshCw, RotateCcw, Square } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiGet, apiPost } from "../api";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Textarea } from "../components/ui/textarea";

type TelegramTarget = {
  id: number;
  name: string;
  identifier: string;
  linked_accounts: Array<{ id: number; label: string }>;
  config: Record<string, unknown>;
  is_active: boolean;
  running: number;
  queued: number;
  failed: number;
  events_count: number;
  last_success_text: string;
  process_text: string;
  process_wait_text?: string;
  ingest_mode: string;
  offset_max_message_id: number;
  offset_accounts_text: string;
};

type TelegramAccount = {
  id: number;
  label: string;
  is_active: boolean;
  utilization: number;
  queued_jobs: number;
  dialogs_count: number;
  parallel_jobs: number;
  backfill_parallel_jobs: number;
  last_success_text: string;
  session_alive: boolean | null;
  session_status_text: string;
  session_last_check_text: string;
  phone: string | null;
  username: string | null;
  telegram_user_id: number | null;
  session_error: string | null;
  activity_text: string;
};

type TelegramJob = {
  id: number;
  target_id: number;
  target_name: string;
  account_id: number | null;
  account_label: string;
  status: string;
  job_type: string;
  attempt: number;
  error: string | null;
  wait_reason: string;
  updated_at: string | null;
  updated_at_text: string;
  error_at_text: string;
};

type TelegramErrorLogRow = {
  id: number;
  target_id: number;
  target_name: string;
  account_id: number | null;
  account_label: string;
  status: string;
  job_type: string;
  attempt: number;
  error: string;
  error_at: string | null;
  error_at_text: string;
};

type AccountDialog = {
  dialog_id: number;
  title: string;
  identifier: string;
  username: string | null;
  kind: string;
  is_linked: boolean;
  target_id: number | null;
  target_name: string | null;
};

type TelegramModuleResponse = {
  summary: {
    targets: number;
    accounts: number;
    events_count: number;
    problem_jobs: number;
  };
  targets: TelegramTarget[];
  accounts: TelegramAccount[];
  jobs: TelegramJob[];
};

type TelegramAuthPayload = {
  api_id: string;
  api_hash: string;
  phone: string;
  temp_session_string: string;
  phone_code_hash: string;
};

function toLocalDatetimeInput(value: string | null | undefined): string {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function toIsoDatetime(value: string): string {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toISOString();
}

export function TelegramPage() {
  const [data, setData] = useState<TelegramModuleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pendingTargetId, setPendingTargetId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const [accountLabel, setAccountLabel] = useState("");
  const [accountApiId, setAccountApiId] = useState("");
  const [accountApiHash, setAccountApiHash] = useState("");
  const [accountPhone, setAccountPhone] = useState("");
  const [accountHourlyLimit, setAccountHourlyLimit] = useState("120");
  const [authCode, setAuthCode] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authLoading, setAuthLoading] = useState(false);
  const [pendingAuthPayload, setPendingAuthPayload] = useState<TelegramAuthPayload | null>(null);

  const [targetName, setTargetName] = useState("");
  const [targetIdentifier, setTargetIdentifier] = useState("");
  const [targetLimit, setTargetLimit] = useState("200");
  const [targetPollInterval, setTargetPollInterval] = useState("300");
  const [targetLiveEnabled, setTargetLiveEnabled] = useState(true);
  const [targetBackfillEnabled, setTargetBackfillEnabled] = useState(false);
  const [targetBackfillMode, setTargetBackfillMode] = useState<"range" | "full">("range");
  const [targetBackfillFrom, setTargetBackfillFrom] = useState("");
  const [targetBackfillTo, setTargetBackfillTo] = useState("");
  const [targetIsRisky, setTargetIsRisky] = useState(false);
  const [targetRiskLabel, setTargetRiskLabel] = useState("");
  const [targetSubmitting, setTargetSubmitting] = useState(false);

  const [linkTargetId, setLinkTargetId] = useState("");
  const [linkAccountId, setLinkAccountId] = useState("");
  const [linkSubmitting, setLinkSubmitting] = useState(false);

  const [serviceLoading, setServiceLoading] = useState("");
  const [retryingTargetId, setRetryingTargetId] = useState<number | null>(null);
  const [retryingJobId, setRetryingJobId] = useState<number | null>(null);

  const [editOpen, setEditOpen] = useState(false);
  const [editTargetId, setEditTargetId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editIdentifier, setEditIdentifier] = useState("");
  const [editLimit, setEditLimit] = useState("200");
  const [editPollInterval, setEditPollInterval] = useState("300");
  const [editLiveEnabled, setEditLiveEnabled] = useState(true);
  const [editBackfillEnabled, setEditBackfillEnabled] = useState(false);
  const [editBackfillMode, setEditBackfillMode] = useState<"range" | "full">("range");
  const [editBackfillFrom, setEditBackfillFrom] = useState("");
  const [editBackfillTo, setEditBackfillTo] = useState("");
  const [editCommentsEnabled, setEditCommentsEnabled] = useState(true);
  const [editGapfillCommentsEnabled, setEditGapfillCommentsEnabled] = useState(true);
  const [editBackfillCommentsEnabled, setEditBackfillCommentsEnabled] = useState(true);
  const [editCommentsLimit, setEditCommentsLimit] = useState("20");
  const [editCommentsDepth, setEditCommentsDepth] = useState("2");
  const [editCommentsRecheckPosts, setEditCommentsRecheckPosts] = useState("30");
  const [editIsRisky, setEditIsRisky] = useState(false);
  const [editRiskLabel, setEditRiskLabel] = useState("");
  const [editSubmitting, setEditSubmitting] = useState(false);

  const [smartAddText, setSmartAddText] = useState("");
  const [smartAddLoading, setSmartAddLoading] = useState(false);
  const [smartRunNow, setSmartRunNow] = useState(true);
  const [smartLiveEnabled, setSmartLiveEnabled] = useState(true);
  const [smartLimit, setSmartLimit] = useState("200");
  const [smartPollInterval, setSmartPollInterval] = useState("300");

  const [dialogsOpen, setDialogsOpen] = useState(false);
  const [dialogsLoading, setDialogsLoading] = useState(false);
  const [dialogsError, setDialogsError] = useState("");
  const [dialogsFilter, setDialogsFilter] = useState("");
  const [activeAccountId, setActiveAccountId] = useState<number | null>(null);
  const [activeAccountLabel, setActiveAccountLabel] = useState("");
  const [accountDialogs, setAccountDialogs] = useState<AccountDialog[]>([]);
  const [dialogActionKey, setDialogActionKey] = useState<string>("");
  const [editAccountOpen, setEditAccountOpen] = useState(false);
  const [editAccountId, setEditAccountId] = useState<number | null>(null);
  const [editAccountLabel, setEditAccountLabel] = useState("");
  const [editAccountSubmitting, setEditAccountSubmitting] = useState(false);
  const [errorLogOpen, setErrorLogOpen] = useState(false);
  const [errorLogLoading, setErrorLogLoading] = useState(false);
  const [errorLogError, setErrorLogError] = useState("");
  const [errorLogRows, setErrorLogRows] = useState<TelegramErrorLogRow[]>([]);
  const [errorLogTargetId, setErrorLogTargetId] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const response = await apiGet<TelegramModuleResponse>("/api/modules/telegram");
      setData(response);
      setError("");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити Telegram модуль");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      void load();
    }, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const callTargetAction = async (targetId: number, action: "run-now" | "stop" | "start") => {
    setPendingTargetId(targetId);
    setError("");
    try {
      await apiPost<{ ok: boolean }>(`/api/modules/telegram/targets/${targetId}/${action}`);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося виконати дію");
      }
    } finally {
      setPendingTargetId(null);
    }
  };

  const startAccountAuth = async () => {
    if (!accountApiId.trim() || !accountApiHash.trim() || !accountPhone.trim()) {
      setError("Для підключення акаунта заповни api_id, api_hash і phone");
      return;
    }

    setAuthLoading(true);
    setError("");
    try {
      const response = await apiPost<{ ok: boolean; auth_payload: TelegramAuthPayload }>(
        "/api/modules/telegram/accounts/start-auth",
        {
          api_id: accountApiId.trim(),
          api_hash: accountApiHash.trim(),
          phone: accountPhone.trim(),
        }
      );
      setPendingAuthPayload(response.auth_payload);
      setAuthCode("");
      setAuthPassword("");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося надіслати код Telegram");
      }
    } finally {
      setAuthLoading(false);
    }
  };

  const completeAccountAuth = async () => {
    if (!pendingAuthPayload) {
      setError("Спочатку натисни 'Надіслати код'");
      return;
    }
    if (!accountLabel.trim()) {
      setError("Вкажи мітку акаунта");
      return;
    }
    if (!authCode.trim()) {
      setError("Введи код із Telegram");
      return;
    }

    setAuthLoading(true);
    setError("");
    try {
      await apiPost<{ ok: boolean; account_id: number; label: string }>("/api/modules/telegram/accounts/complete-auth", {
        label: accountLabel.trim(),
        hourly_limit: Number(accountHourlyLimit) || 120,
        api_id: pendingAuthPayload.api_id,
        api_hash: pendingAuthPayload.api_hash,
        phone: pendingAuthPayload.phone,
        temp_session_string: pendingAuthPayload.temp_session_string,
        phone_code_hash: pendingAuthPayload.phone_code_hash,
        code: authCode.trim(),
        password: authPassword.trim(),
      });
      setPendingAuthPayload(null);
      setAuthCode("");
      setAuthPassword("");
      setAccountLabel("");
      setAccountApiId("");
      setAccountApiHash("");
      setAccountPhone("");
      setAccountHourlyLimit("120");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завершити авторизацію Telegram");
      }
    } finally {
      setAuthLoading(false);
    }
  };

  const createTarget = async () => {
    if (!targetIdentifier.trim()) {
      setError("Вкажи канал або чат для створення цілі");
      return;
    }
    setTargetSubmitting(true);
    setError("");
    try {
      await apiPost<{ id: number }>("/api/targets", {
        parser_type: "telegram",
        name: targetName.trim() || targetIdentifier.trim(),
        identifier: targetIdentifier.trim(),
        config: {
          limit: Number(targetLimit) || 200,
          poll_interval_seconds: Number(targetPollInterval) || 300,
          live_enabled: targetLiveEnabled,
          gapfill_limit: Number(targetLimit) || 200,
          backfill_limit: 5000,
          participants_sync_enabled: true,
          participants_sync_interval_seconds: 3600,
          participants_limit: 1000,
          comments_enabled: true,
          gapfill_comments_enabled: true,
          backfill_comments_enabled: true,
          comments_limit: 20,
          comments_depth: 2,
          comments_recheck_posts: 30,
          is_risky: targetIsRisky,
          risk_label: targetRiskLabel.trim(),
          backfill: {
            enabled: targetBackfillEnabled,
            mode: targetBackfillMode,
            from: targetBackfillMode === "range" ? toIsoDatetime(targetBackfillFrom) || null : null,
            to: targetBackfillMode === "range" ? toIsoDatetime(targetBackfillTo) || null : null,
            chunk_days: 7,
          },
        },
      });
      setTargetName("");
      setTargetIdentifier("");
      setTargetLimit("200");
      setTargetPollInterval("300");
      setTargetLiveEnabled(true);
      setTargetBackfillEnabled(false);
      setTargetBackfillMode("range");
      setTargetBackfillFrom("");
      setTargetBackfillTo("");
      setTargetIsRisky(false);
      setTargetRiskLabel("");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити ціль");
      }
    } finally {
      setTargetSubmitting(false);
    }
  };

  const createLink = async () => {
    const targetId = Number(linkTargetId);
    const accountId = Number(linkAccountId);
    if (!targetId || !accountId) {
      setError("Вкажи ціль та акаунт для прив'язки");
      return;
    }
    setLinkSubmitting(true);
    setError("");
    try {
      await apiPost<{ ok: boolean }>("/api/links", { target_id: targetId, account_id: accountId });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити прив'язку");
      }
    } finally {
      setLinkSubmitting(false);
    }
  };

  const runServiceAction = async (
    key: string,
    path: string,
    body: Record<string, unknown> | undefined = undefined
  ) => {
    setServiceLoading(key);
    setError("");
    try {
      await apiPost(path, body);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося виконати сервісну дію");
      }
    } finally {
      setServiceLoading("");
    }
  };

  const retryTargetFailedJobs = async (targetId: number) => {
    setRetryingTargetId(targetId);
    setError("");
    try {
      await apiPost<{ ok: boolean; updated: number }>("/api/modules/telegram/jobs/retry-failed", { target_id: targetId });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося перезапустити помилкові задачі цілі");
      }
    } finally {
      setRetryingTargetId(null);
    }
  };

  const retryJob = async (jobId: number) => {
    setRetryingJobId(jobId);
    setError("");
    try {
      await apiPost<{ ok: boolean }>(`/api/modules/telegram/jobs/${jobId}/retry`);
      await load();
      if (errorLogOpen) {
        const parsedTargetId = Number(errorLogTargetId);
        const targetId = Number.isFinite(parsedTargetId) && parsedTargetId > 0 ? parsedTargetId : null;
        await loadErrorLog(targetId);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося перезапустити задачу");
      }
    } finally {
      setRetryingJobId(null);
    }
  };

  const openEditTarget = (target: TelegramTarget) => {
    const cfg = (target.config || {}) as Record<string, unknown>;
    const backfill = (cfg.backfill || {}) as Record<string, unknown>;
    const modeRaw = String(backfill.mode || "range").toLowerCase();
    const mode: "range" | "full" = modeRaw === "full" ? "full" : "range";
    setEditTargetId(target.id);
    setEditName(target.name);
    setEditIdentifier(target.identifier);
    setEditLimit(String(cfg.limit ?? 200));
    setEditPollInterval(String(cfg.poll_interval_seconds ?? 300));
    setEditLiveEnabled(Boolean(cfg.live_enabled ?? true));
    setEditBackfillEnabled(Boolean(backfill.enabled ?? false));
    setEditBackfillMode(mode);
    setEditBackfillFrom(toLocalDatetimeInput(String(backfill.from || "")));
    setEditBackfillTo(toLocalDatetimeInput(String(backfill.to || "")));
    setEditCommentsEnabled(Boolean(cfg.comments_enabled ?? true));
    setEditGapfillCommentsEnabled(Boolean(cfg.gapfill_comments_enabled ?? true));
    setEditBackfillCommentsEnabled(Boolean(cfg.backfill_comments_enabled ?? true));
    setEditCommentsLimit(String(cfg.comments_limit ?? 20));
    setEditCommentsDepth(String(cfg.comments_depth ?? 2));
    setEditCommentsRecheckPosts(String(cfg.comments_recheck_posts ?? 30));
    setEditIsRisky(Boolean(cfg.is_risky ?? false));
    setEditRiskLabel(String(cfg.risk_label ?? ""));
    setEditOpen(true);
  };

  const saveEditTarget = async () => {
    if (!editTargetId) {
      return;
    }
    setEditSubmitting(true);
    setError("");
    try {
      await apiPost<{ ok: boolean }>(`/api/modules/telegram/targets/${editTargetId}/update`, {
        name: editName.trim() || undefined,
        identifier: editIdentifier.trim() || undefined,
        limit: Number(editLimit) || 200,
        poll_interval_seconds: Number(editPollInterval) || 300,
        live_enabled: editLiveEnabled,
        backfill_enabled: editBackfillEnabled,
        backfill_mode: editBackfillMode,
        backfill_from: editBackfillMode === "range" ? toIsoDatetime(editBackfillFrom) : "",
        backfill_to: editBackfillMode === "range" ? toIsoDatetime(editBackfillTo) : "",
        comments_enabled: editCommentsEnabled,
        gapfill_comments_enabled: editGapfillCommentsEnabled,
        backfill_comments_enabled: editBackfillCommentsEnabled,
        comments_limit: Number(editCommentsLimit) || 20,
        comments_depth: Number(editCommentsDepth) || 2,
        comments_recheck_posts: Number(editCommentsRecheckPosts) || 30,
        is_risky: editIsRisky,
        risk_label: editRiskLabel.trim(),
      });
      setEditOpen(false);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося оновити ціль");
      }
    } finally {
      setEditSubmitting(false);
    }
  };

  const handleSmartAdd = async () => {
    if (!smartAddText.trim()) {
      setError("Вкажи хоча б один канал/чат у полі розумного додавання");
      return;
    }

    setSmartAddLoading(true);
    setError("");
    try {
      await apiPost("/api/modules/telegram/targets/smart-add", {
        entries_text: smartAddText,
        run_now: smartRunNow,
        live_enabled: smartLiveEnabled,
        limit: Number(smartLimit) || 200,
        poll_interval_seconds: Number(smartPollInterval) || 300,
      });
      setSmartAddText("");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося виконати розумне додавання");
      }
    } finally {
      setSmartAddLoading(false);
    }
  };

  const activeJobs = useMemo(() => data?.jobs.filter((job) => job.status !== "succeeded") ?? [], [data?.jobs]);
  const loadErrorLog = useCallback(
    async (targetId: number | null = null) => {
      setErrorLogLoading(true);
      setErrorLogError("");
      try {
        const params = new URLSearchParams();
        params.set("limit", "300");
        if (targetId && targetId > 0) {
          params.set("target_id", String(targetId));
        }
        const response = await apiGet<{ rows: TelegramErrorLogRow[] }>(`/api/modules/telegram/jobs/error-log?${params.toString()}`);
        setErrorLogRows(response.rows ?? []);
      } catch (err) {
        if (err instanceof ApiError) {
          setErrorLogError(err.message);
        } else {
          setErrorLogError("Не вдалося завантажити журнал помилок");
        }
      } finally {
        setErrorLogLoading(false);
      }
    },
    []
  );

  const openErrorLog = async () => {
    setErrorLogOpen(true);
    const parsedTargetId = Number(errorLogTargetId);
    const targetId = Number.isFinite(parsedTargetId) && parsedTargetId > 0 ? parsedTargetId : null;
    await loadErrorLog(targetId);
  };

  const filteredDialogs = useMemo(() => {
    const q = dialogsFilter.trim().toLowerCase();
    if (!q) {
      return accountDialogs;
    }
    return accountDialogs.filter((item) => {
      return (
        String(item.title || "").toLowerCase().includes(q)
        || String(item.identifier || "").toLowerCase().includes(q)
        || String(item.kind || "").toLowerCase().includes(q)
      );
    });
  }, [accountDialogs, dialogsFilter]);

  const loadAccountDialogs = useCallback(
    async (accountId: number, refresh = false) => {
      setDialogsLoading(true);
      setDialogsError("");
      try {
        const query = refresh ? "?refresh=true" : "";
        const response = await apiGet<{ account: { id: number; label: string }; dialogs: AccountDialog[] }>(
          `/api/modules/telegram/accounts/${accountId}/dialogs${query}`
        );
        setActiveAccountId(response.account.id);
        setActiveAccountLabel(response.account.label);
        setAccountDialogs(response.dialogs ?? []);
      } catch (err) {
        if (err instanceof ApiError) {
          setDialogsError(err.message);
        } else {
          setDialogsError("Не вдалося завантажити чати акаунта");
        }
      } finally {
        setDialogsLoading(false);
      }
    },
    []
  );

  const openAccountDialogs = async (accountId: number, accountLabel: string) => {
    setDialogsOpen(true);
    setActiveAccountId(accountId);
    setActiveAccountLabel(accountLabel);
    setDialogsFilter("");
    await loadAccountDialogs(accountId, true);
  };

  const openEditAccount = (account: TelegramAccount) => {
    setEditAccountId(account.id);
    setEditAccountLabel(account.label);
    setEditAccountOpen(true);
  };

  const saveAccountLabel = async () => {
    if (!editAccountId) {
      return;
    }
    const nextLabel = editAccountLabel.trim();
    if (!nextLabel) {
      setError("Мітка акаунта не може бути порожньою");
      return;
    }

    setEditAccountSubmitting(true);
    setError("");
    try {
      await apiPost<{ ok: boolean; account_id: number; label: string }>(`/api/modules/telegram/accounts/${editAccountId}/update-label`, {
        label: nextLabel,
      });
      setEditAccountOpen(false);
      if (activeAccountId === editAccountId) {
        setActiveAccountLabel(nextLabel);
      }
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося оновити мітку акаунта");
      }
    } finally {
      setEditAccountSubmitting(false);
    }
  };

  const addDialogAsTargetAndRun = async (dialog: AccountDialog) => {
    if (!activeAccountId) {
      return;
    }
    const key = `${dialog.identifier}:${activeAccountId}`;
    setDialogActionKey(key);
    setDialogsError("");
    try {
      await apiPost(`/api/modules/telegram/accounts/${activeAccountId}/dialogs/add-target`, {
        identifier: dialog.identifier,
        title: dialog.title,
        run_now: true,
      });
      await Promise.all([load(), loadAccountDialogs(activeAccountId, true)]);
    } catch (err) {
      if (err instanceof ApiError) {
        setDialogsError(err.message);
      } else {
        setDialogsError("Не вдалося додати чат у парсинг");
      }
    } finally {
      setDialogActionKey("");
    }
  };

  useEffect(() => {
    if (!dialogsOpen || !activeAccountId) {
      return;
    }
    const timer = setInterval(() => {
      if (!dialogActionKey) {
        void loadAccountDialogs(activeAccountId, true);
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [dialogsOpen, activeAccountId, dialogActionKey, loadAccountDialogs]);

  useEffect(() => {
    if (!errorLogOpen) {
      return;
    }
    const parsedTargetId = Number(errorLogTargetId);
    const targetId = Number.isFinite(parsedTargetId) && parsedTargetId > 0 ? parsedTargetId : null;
    void loadErrorLog(targetId);
    const timer = setInterval(() => {
      void loadErrorLog(targetId);
    }, 15000);
    return () => clearInterval(timer);
  }, [errorLogOpen, errorLogTargetId, loadErrorLog]);

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Telegram модуль</CardTitle>
          <CardDescription>Керування парсингом Telegram.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-md border bg-muted p-3">
              <p className="text-xs text-muted-foreground">Цілей</p>
              <p className="text-2xl font-semibold">{data?.summary.targets ?? 0}</p>
            </div>
            <div className="rounded-md border bg-muted p-3">
              <p className="text-xs text-muted-foreground">Акаунтів</p>
              <p className="text-2xl font-semibold">{data?.summary.accounts ?? 0}</p>
            </div>
            <div className="rounded-md border bg-muted p-3">
              <p className="text-xs text-muted-foreground">Збережено повідомлень</p>
              <p className="text-2xl font-semibold">{data?.summary.events_count ?? 0}</p>
            </div>
            <div className="rounded-md border bg-muted p-3">
              <p className="text-xs text-muted-foreground">Проблемних задач</p>
              <p className="text-2xl font-semibold">{data?.summary.problem_jobs ?? 0}</p>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Підключити Telegram-акаунт</CardTitle>
          <CardDescription>Авторизація через код Telegram (без session string).</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-5">
            <div className="space-y-1">
              <Label htmlFor="acc-label">Мітка акаунта</Label>
              <Input id="acc-label" value={accountLabel} onChange={(event) => setAccountLabel(event.target.value)} placeholder="my_tg_1" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-api-id">API ID</Label>
              <Input id="acc-api-id" value={accountApiId} onChange={(event) => setAccountApiId(event.target.value)} placeholder="1234567" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-api-hash">API Hash</Label>
              <Input id="acc-api-hash" value={accountApiHash} onChange={(event) => setAccountApiHash(event.target.value)} placeholder="xxxxxxxx" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-phone">Телефон</Label>
              <Input id="acc-phone" value={accountPhone} onChange={(event) => setAccountPhone(event.target.value)} placeholder="+380..." />
            </div>
            <div className="space-y-1">
              <Label htmlFor="acc-hourly">Ліміт/год</Label>
              <Input
                id="acc-hourly"
                type="number"
                min={1}
                value={accountHourlyLimit}
                onChange={(event) => setAccountHourlyLimit(event.target.value)}
              />
            </div>
          </div>

          <div className="flex items-center justify-end">
            <Button type="button" variant="outline" onClick={() => void startAccountAuth()} disabled={authLoading}>
              {authLoading ? "Надсилаю..." : "Надіслати код"}
            </Button>
          </div>

          {pendingAuthPayload ? (
            <div className="space-y-3 rounded-md border p-3">
              <p className="text-sm text-muted-foreground">Код надіслано на {pendingAuthPayload.phone}. Введи код для завершення.</p>
              <div className="grid gap-3 md:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="acc-code">Код із Telegram</Label>
                  <Input id="acc-code" value={authCode} onChange={(event) => setAuthCode(event.target.value)} placeholder="12345" />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="acc-pass">Пароль 2FA Telegram (якщо є)</Label>
                  <Input
                    id="acc-pass"
                    type="password"
                    value={authPassword}
                    onChange={(event) => setAuthPassword(event.target.value)}
                    placeholder="Необов'язково"
                  />
                </div>
              </div>
              <div className="flex items-center justify-end gap-2">
                <Button type="button" variant="ghost" onClick={() => setPendingAuthPayload(null)} disabled={authLoading}>
                  Скасувати
                </Button>
                <Button type="button" onClick={() => void completeAccountAuth()} disabled={authLoading}>
                  {authLoading ? "Підключаю..." : "Підключити акаунт"}
                </Button>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Додати канал / чат вручну</CardTitle>
          <CardDescription>Створення цілі з базовими налаштуваннями парсингу.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="target-name">Назва</Label>
              <Input id="target-name" value={targetName} onChange={(event) => setTargetName(event.target.value)} placeholder="@my_channel" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="target-identifier">Канал / чат</Label>
              <Input
                id="target-identifier"
                value={targetIdentifier}
                onChange={(event) => setTargetIdentifier(event.target.value)}
                placeholder="@channel або https://t.me/channel"
              />
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="space-y-1">
              <Label htmlFor="target-limit">Ліміт повідомлень</Label>
              <Input id="target-limit" type="number" min={1} value={targetLimit} onChange={(event) => setTargetLimit(event.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="target-poll">Інтервал (сек)</Label>
              <Input
                id="target-poll"
                type="number"
                min={30}
                value={targetPollInterval}
                onChange={(event) => setTargetPollInterval(event.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={targetLiveEnabled}
                onChange={(event) => setTargetLiveEnabled(event.target.checked)}
              />
              Увімкнути live
            </label>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={targetBackfillEnabled}
                onChange={(event) => setTargetBackfillEnabled(event.target.checked)}
              />
              Увімкнути backfill
            </label>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={targetIsRisky}
                onChange={(event) => setTargetIsRisky(event.target.checked)}
              />
              Позначити як ризикову ціль
            </label>
            <div className="space-y-1">
              <Label htmlFor="target-risk-label">Мітка ризику</Label>
              <Input
                id="target-risk-label"
                value={targetRiskLabel}
                onChange={(event) => setTargetRiskLabel(event.target.value)}
                placeholder="шахрайство / scam / carding"
              />
            </div>
          </div>
          {targetBackfillEnabled ? (
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="target-backfill-mode">Режим backfill</Label>
                <select
                  id="target-backfill-mode"
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                  value={targetBackfillMode}
                  onChange={(event) => setTargetBackfillMode(event.target.value === "full" ? "full" : "range")}
                >
                  <option value="range">Діапазон дат</option>
                  <option value="full">Повна історія</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="target-backfill-from">Початок (календар)</Label>
                <Input
                  id="target-backfill-from"
                  type="datetime-local"
                  value={targetBackfillFrom}
                  onChange={(event) => setTargetBackfillFrom(event.target.value)}
                  disabled={targetBackfillMode === "full"}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="target-backfill-to">Кінець (календар)</Label>
                <Input
                  id="target-backfill-to"
                  type="datetime-local"
                  value={targetBackfillTo}
                  onChange={(event) => setTargetBackfillTo(event.target.value)}
                  disabled={targetBackfillMode === "full"}
                />
              </div>
            </div>
          ) : null}
          <div className="flex items-center justify-end">
            <Button type="button" onClick={() => void createTarget()} disabled={targetSubmitting}>
              {targetSubmitting ? "Створюю..." : "Додати ціль"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Прив'язати акаунт до цілі</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="link-target">Ціль</Label>
              <select
                id="link-target"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={linkTargetId}
                onChange={(event) => setLinkTargetId(event.target.value)}
              >
                <option value="">Оберіть ціль</option>
                {(data?.targets || []).map((target) => (
                  <option key={target.id} value={target.id}>
                    {target.name} ({target.identifier})
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="link-account">Акаунт</Label>
              <select
                id="link-account"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={linkAccountId}
                onChange={(event) => setLinkAccountId(event.target.value)}
              >
                <option value="">Оберіть акаунт</option>
                {(data?.accounts || []).map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex items-center justify-end">
            <Button type="button" variant="outline" onClick={() => void createLink()} disabled={linkSubmitting}>
              {linkSubmitting ? "Прив'язую..." : "Створити прив'язку"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Сервісні дії</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => void runServiceAction("scheduler", "/api/scheduler/run")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "scheduler" ? "Запускаю..." : "Запустити планувальник"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void runServiceAction("retry", "/api/modules/telegram/jobs/retry-failed")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "retry" ? "Повторюю..." : "Повторити помилки"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void runServiceAction("cooldown", "/api/modules/telegram/accounts/reset-cooldown")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "cooldown" ? "Скидаю..." : "Скинути паузу акаунтів"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void runServiceAction("members", "/api/telegram/sync-memberships")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "members" ? "Синхронізую..." : "Синхронізувати доступ акаунтів"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void runServiceAction("status", "/api/modules/telegram/accounts/refresh-status")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "status" ? "Оновлюю..." : "Оновити живість акаунтів"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void runServiceAction("comments-defaults", "/api/modules/telegram/targets/enable-comments-defaults")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "comments-defaults" ? "Оновлюю..." : "Увімкнути коментарі для всіх цілей"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Розумне додавання кількох цілей</CardTitle>
          <CardDescription>
            Встав кілька @username або посилань. Система автоматично розподілить їх між активними акаунтами.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="smart-add-input">Канали/чати (кожен з нового рядка або через кому)</Label>
            <Textarea
              id="smart-add-input"
              value={smartAddText}
              onChange={(event) => setSmartAddText(event.target.value)}
              placeholder="@channel_one\nhttps://t.me/channel_two\n@chat_three"
              rows={5}
            />
          </div>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="space-y-1">
              <Label htmlFor="smart-limit">Ліміт повідомлень</Label>
              <Input
                id="smart-limit"
                type="number"
                min={1}
                value={smartLimit}
                onChange={(event) => setSmartLimit(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="smart-poll">Інтервал (сек)</Label>
              <Input
                id="smart-poll"
                type="number"
                min={30}
                value={smartPollInterval}
                onChange={(event) => setSmartPollInterval(event.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={smartLiveEnabled}
                onChange={(event) => setSmartLiveEnabled(event.target.checked)}
              />
              Увімкнути live
            </label>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={smartRunNow}
                onChange={(event) => setSmartRunNow(event.target.checked)}
              />
              Запустити одразу
            </label>
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setSmartAddText("")} disabled={smartAddLoading}>
              Очистити
            </Button>
            <Button type="button" onClick={() => void handleSmartAdd()} disabled={smartAddLoading}>
              {smartAddLoading ? "Додаю..." : "Додати цілі"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle className="text-lg">Список каналів і чатів</CardTitle>
          </div>
          <Button type="button" variant="secondary" onClick={() => void load()} disabled={loading}>
            Оновити
          </Button>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Назва</TableHead>
                <TableHead>Канал / чат</TableHead>
                <TableHead>Привʼязаний акаунт</TableHead>
                <TableHead>Стан</TableHead>
                <TableHead>Процес парсингу</TableHead>
                <TableHead>Збережено повідомлень</TableHead>
                <TableHead>Останній успішний запуск</TableHead>
                <TableHead>Дії</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.targets.map((target) => (
                <TableRow key={target.id}>
                  <TableCell>{target.id}</TableCell>
                  <TableCell>
                    <div className="space-y-1">
                      <div>{target.name}</div>
                      {Boolean((target.config || {}).is_risky) ? (
                        <Badge variant="destructive">
                          Ризик{String((target.config || {}).risk_label || "").trim() ? `: ${String((target.config || {}).risk_label || "").trim()}` : ""}
                        </Badge>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell>{target.identifier}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {(target.linked_accounts || []).length > 0 ? (
                        (target.linked_accounts || []).map((account) => (
                          <Badge key={`${target.id}:${account.id}`} variant="outline">
                            {account.label}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={target.is_active ? "secondary" : "outline"}>{target.is_active ? "Увімкнено" : "Вимкнено"}</Badge>
                  </TableCell>
                  <TableCell className="space-y-1">
                    <p>{target.process_text}</p>
                    {target.process_wait_text ? <p className="text-xs text-amber-700">{target.process_wait_text}</p> : null}
                    <p className="text-xs text-muted-foreground">Режим: {target.ingest_mode}</p>
                    <p className="text-xs text-muted-foreground">
                      Offset: {target.offset_max_message_id} | Синхронно: {target.offset_accounts_text}
                    </p>
                  </TableCell>
                  <TableCell>{target.events_count}</TableCell>
                  <TableCell>{target.last_success_text}</TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Button
                        size="icon"
                        variant="secondary"
                        type="button"
                        disabled={pendingTargetId === target.id}
                        onClick={() => void callTargetAction(target.id, "run-now")}
                        title="Оновити зараз"
                      >
                        <RefreshCw className="h-4 w-4" />
                      </Button>
                      {target.failed > 0 ? (
                        <Button
                          size="icon"
                          variant="outline"
                          type="button"
                          disabled={retryingTargetId === target.id}
                          onClick={() => void retryTargetFailedJobs(target.id)}
                          title="Перезапустити помилкові задачі цілі"
                        >
                          <RotateCcw className="h-4 w-4" />
                        </Button>
                      ) : null}
                      {target.is_active ? (
                        <Button
                          size="icon"
                          variant="destructive"
                          type="button"
                          disabled={pendingTargetId === target.id}
                          onClick={() => void callTargetAction(target.id, "stop")}
                          title="Зупинити"
                        >
                          <Square className="h-4 w-4" />
                        </Button>
                      ) : (
                        <Button
                          size="icon"
                          variant="secondary"
                          type="button"
                          disabled={pendingTargetId === target.id}
                          onClick={() => void callTargetAction(target.id, "start")}
                          title="Увімкнути"
                        >
                          <Play className="h-4 w-4" />
                        </Button>
                      )}
                      <Button
                        size="icon"
                        variant="outline"
                        type="button"
                        onClick={() => openEditTarget(target)}
                        title="Редагувати"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {data && data.targets.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9}>Цілі відсутні.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <section className="grid gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Акаунти</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Мітка</TableHead>
                  <TableHead>Сесія</TableHead>
                  <TableHead>Активність</TableHead>
                  <TableHead>Дані акаунта</TableHead>
                  <TableHead>Завантаженість</TableHead>
                  <TableHead>Черга</TableHead>
                  <TableHead>Паралельність</TableHead>
                  <TableHead>Канали/чати</TableHead>
                  <TableHead>Останній успіх</TableHead>
                  <TableHead>Дії</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.accounts.map((account) => (
                  <TableRow key={account.id}>
                    <TableCell>{account.id}</TableCell>
                    <TableCell>{account.label}</TableCell>
                    <TableCell>
                      <div className="space-y-1">
                        <Badge
                          variant={
                            account.session_alive === true
                              ? "secondary"
                              : account.session_alive === false
                                ? "destructive"
                                : "outline"
                          }
                        >
                          {account.session_status_text}
                        </Badge>
                        <p className="text-xs text-muted-foreground">Перевірка: {account.session_last_check_text}</p>
                        {account.session_error ? <p className="text-xs text-destructive">{account.session_error}</p> : null}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="space-y-1">
                        <p className="text-sm">{account.activity_text || "-"}</p>
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="space-y-1 text-xs">
                        <p>{account.phone ? `Телефон: ${account.phone}` : "Телефон: -"}</p>
                        <p>{account.username ? `Username: ${account.username}` : "Username: -"}</p>
                        <p>{account.telegram_user_id ? `User ID: ${account.telegram_user_id}` : "User ID: -"}</p>
                      </div>
                    </TableCell>
                    <TableCell>{account.utilization}%</TableCell>
                    <TableCell>{account.queued_jobs}</TableCell>
                    <TableCell>
                      {account.parallel_jobs}/{account.backfill_parallel_jobs}
                    </TableCell>
                    <TableCell>{account.dialogs_count}</TableCell>
                    <TableCell>{account.last_success_text}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Button size="sm" variant="outline" onClick={() => void openAccountDialogs(account.id, account.label)}>
                          Відкрити чати
                        </Button>
                        <Button
                          size="icon"
                          variant="outline"
                          type="button"
                          onClick={() => openEditAccount(account)}
                          title="Змінити мітку"
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-lg">Активні та проблемні задачі</CardTitle>
              <Button type="button" variant="outline" size="sm" onClick={() => void openErrorLog()}>
                Журнал помилок
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Ціль</TableHead>
                  <TableHead>Тип задачі</TableHead>
                  <TableHead>Акаунт</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Що очікує</TableHead>
                  <TableHead>Спроба</TableHead>
                  <TableHead>Час помилки</TableHead>
                  <TableHead>Помилка</TableHead>
                  <TableHead>Дії</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {activeJobs.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell>{job.id}</TableCell>
                    <TableCell>{job.target_name}</TableCell>
                    <TableCell>{job.job_type}</TableCell>
                    <TableCell>{job.account_label}</TableCell>
                    <TableCell>{job.status}</TableCell>
                    <TableCell>{job.wait_reason}</TableCell>
                    <TableCell>{job.attempt}</TableCell>
                    <TableCell>{job.status === "failed" || job.status === "retry" ? job.error_at_text || "-" : "-"}</TableCell>
                    <TableCell>{job.status === "failed" || job.status === "retry" ? job.error ?? "-" : "-"}</TableCell>
                    <TableCell>
                      {job.status === "pending" || job.status === "failed" || job.status === "retry" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          type="button"
                          disabled={retryingJobId === job.id}
                          onClick={() => void retryJob(job.id)}
                        >
                          {retryingJobId === job.id
                            ? "Запуск..."
                            : job.status === "pending"
                              ? "Запустити зараз"
                              : "Перезапустити"}
                        </Button>
                      ) : (
                        "-"
                      )}
                    </TableCell>
                  </TableRow>
                ))}
                {activeJobs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={10}>Активних або проблемних задач немає.</TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Редагувати ціль</DialogTitle>
            <DialogDescription>Онови параметри парсингу для каналу/чату.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="edit-target-name">Назва</Label>
                <Input id="edit-target-name" value={editName} onChange={(event) => setEditName(event.target.value)} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="edit-target-identifier">Канал / чат</Label>
                <Input id="edit-target-identifier" value={editIdentifier} onChange={(event) => setEditIdentifier(event.target.value)} />
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="edit-target-limit">Ліміт повідомлень</Label>
                <Input
                  id="edit-target-limit"
                  type="number"
                  min={1}
                  value={editLimit}
                  onChange={(event) => setEditLimit(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="edit-target-poll">Інтервал (сек)</Label>
                <Input
                  id="edit-target-poll"
                  type="number"
                  min={30}
                  value={editPollInterval}
                  onChange={(event) => setEditPollInterval(event.target.value)}
                />
              </div>
              <label className="flex items-center gap-2 text-sm md:pt-7">
                <input
                  type="checkbox"
                  checked={editLiveEnabled}
                  onChange={(event) => setEditLiveEnabled(event.target.checked)}
                />
                Увімкнути live
              </label>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              <label className="flex items-center gap-2 text-sm md:pt-7">
                <input
                  type="checkbox"
                  checked={editBackfillEnabled}
                  onChange={(event) => setEditBackfillEnabled(event.target.checked)}
                />
                Увімкнути backfill
              </label>
              <div className="space-y-1">
                <Label htmlFor="edit-backfill-mode">Режим backfill</Label>
                <select
                  id="edit-backfill-mode"
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                  value={editBackfillMode}
                  onChange={(event) => setEditBackfillMode(event.target.value === "full" ? "full" : "range")}
                >
                  <option value="range">Діапазон дат</option>
                  <option value="full">Повна історія</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="edit-backfill-from">Початок</Label>
                <Input
                  id="edit-backfill-from"
                  type="datetime-local"
                  value={editBackfillFrom}
                  onChange={(event) => setEditBackfillFrom(event.target.value)}
                  disabled={editBackfillMode === "full"}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="edit-backfill-to">Кінець</Label>
                <Input
                  id="edit-backfill-to"
                  type="datetime-local"
                  value={editBackfillTo}
                  onChange={(event) => setEditBackfillTo(event.target.value)}
                  disabled={editBackfillMode === "full"}
                />
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={editIsRisky}
                  onChange={(event) => setEditIsRisky(event.target.checked)}
                />
                Позначити як ризикову ціль
              </label>
              <div className="space-y-1">
                <Label htmlFor="edit-risk-label">Мітка ризику</Label>
                <Input
                  id="edit-risk-label"
                  value={editRiskLabel}
                  onChange={(event) => setEditRiskLabel(event.target.value)}
                  placeholder="шахрайство / scam / carding"
                />
              </div>
            </div>
            <div className="space-y-2 rounded-md border p-3">
              <p className="text-sm font-medium">Коментарі до постів</p>
              <div className="grid gap-3 md:grid-cols-3">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={editCommentsEnabled}
                    onChange={(event) => setEditCommentsEnabled(event.target.checked)}
                  />
                  Увімкнути в live/poll
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={editGapfillCommentsEnabled}
                    onChange={(event) => setEditGapfillCommentsEnabled(event.target.checked)}
                  />
                  Увімкнути в доборі
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={editBackfillCommentsEnabled}
                    onChange={(event) => setEditBackfillCommentsEnabled(event.target.checked)}
                  />
                  Увімкнути в backfill
                </label>
              </div>
              <div className="grid gap-3 md:grid-cols-3">
                <div className="space-y-1">
                  <Label htmlFor="edit-comments-limit">Ліміт коментарів</Label>
                  <Input
                    id="edit-comments-limit"
                    type="number"
                    min={1}
                    value={editCommentsLimit}
                    onChange={(event) => setEditCommentsLimit(event.target.value)}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="edit-comments-depth">Глибина</Label>
                  <Input
                    id="edit-comments-depth"
                    type="number"
                    min={1}
                    value={editCommentsDepth}
                    onChange={(event) => setEditCommentsDepth(event.target.value)}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="edit-comments-recheck">Перевірка кореневих постів</Label>
                  <Input
                    id="edit-comments-recheck"
                    type="number"
                    min={1}
                    value={editCommentsRecheckPosts}
                    onChange={(event) => setEditCommentsRecheckPosts(event.target.value)}
                  />
                </div>
              </div>
            </div>
            <div className="flex items-center justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setEditOpen(false)} disabled={editSubmitting}>
                Скасувати
              </Button>
              <Button type="button" onClick={() => void saveEditTarget()} disabled={editSubmitting || !editTargetId}>
                {editSubmitting ? "Зберігаю..." : "Зберегти"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={dialogsOpen} onOpenChange={setDialogsOpen}>
        <DialogContent className="flex h-[85vh] w-[96vw] max-w-[1200px] flex-col">
          <DialogHeader>
            <DialogTitle>Чати акаунта: {activeAccountLabel || "-"}</DialogTitle>
            <DialogDescription>
              Обери чат/канал і натисни запуск. Якщо ціль не існує, вона буде створена, автоматично привʼязана до цього акаунта й запущена з повним backfill.
            </DialogDescription>
          </DialogHeader>
          <div className="flex min-h-0 flex-1 flex-col gap-3">
            <div className="flex items-center gap-2">
              <Input
                value={dialogsFilter}
                onChange={(event) => setDialogsFilter(event.target.value)}
                placeholder="Пошук по назві, ідентифікатору, типу..."
              />
              <Button
                variant="outline"
                onClick={() => activeAccountId && void loadAccountDialogs(activeAccountId, true)}
                disabled={!activeAccountId || dialogsLoading}
              >
                Оновити список
              </Button>
            </div>
            {dialogsError ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{dialogsError}</div> : null}
            <div className="min-h-0 flex-1 overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Назва</TableHead>
                    <TableHead>Ідентифікатор</TableHead>
                    <TableHead>Тип</TableHead>
                    <TableHead>Стан</TableHead>
                    <TableHead>Дія</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredDialogs.map((dialog) => {
                    const key = `${dialog.identifier}:${activeAccountId ?? 0}`;
                    return (
                      <TableRow key={key}>
                        <TableCell>{dialog.title}</TableCell>
                        <TableCell>{dialog.identifier}</TableCell>
                        <TableCell>{dialog.kind}</TableCell>
                        <TableCell>
                          {dialog.is_linked ? (
                            <Badge variant="secondary">{dialog.target_name ? `Привʼязано: ${dialog.target_name}` : "Привʼязано"}</Badge>
                          ) : (
                            <Badge variant="outline">Не привʼязано</Badge>
                          )}
                        </TableCell>
                        <TableCell>
                          <Button
                            size="sm"
                            variant={dialog.is_linked ? "secondary" : "default"}
                            disabled={dialogActionKey === key}
                            onClick={() => void addDialogAsTargetAndRun(dialog)}
                          >
                            {dialogActionKey === key ? "Запуск..." : dialog.is_linked ? "Запустити" : "Додати і запустити"}
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                  {!dialogsLoading && filteredDialogs.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={5}>Чати не знайдено.</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={editAccountOpen} onOpenChange={setEditAccountOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Змінити мітку акаунта</DialogTitle>
            <DialogDescription>Нова мітка для Telegram-акаунта.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="edit-account-label">Мітка</Label>
              <Input
                id="edit-account-label"
                value={editAccountLabel}
                onChange={(event) => setEditAccountLabel(event.target.value)}
                placeholder="my_tg_1"
              />
            </div>
            <div className="flex items-center justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setEditAccountOpen(false)} disabled={editAccountSubmitting}>
                Скасувати
              </Button>
              <Button type="button" onClick={() => void saveAccountLabel()} disabled={editAccountSubmitting || !editAccountId}>
                {editAccountSubmitting ? "Зберігаю..." : "Зберегти"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={errorLogOpen} onOpenChange={setErrorLogOpen}>
        <DialogContent className="flex h-[80vh] w-[96vw] max-w-[1300px] flex-col">
          <DialogHeader>
            <DialogTitle>Журнал помилок Telegram</DialogTitle>
            <DialogDescription>Окремий список помилок задач (failed/retry) із часом помилки.</DialogDescription>
          </DialogHeader>
          <div className="flex min-h-0 flex-1 flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <select
                className="h-9 w-full rounded-md border bg-background px-3 text-sm sm:w-[360px]"
                value={errorLogTargetId}
                onChange={(event) => setErrorLogTargetId(event.target.value)}
              >
                <option value="">Усі цілі</option>
                {(data?.targets || []).map((target) => (
                  <option key={target.id} value={target.id}>
                    {target.name} ({target.identifier})
                  </option>
                ))}
              </select>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  const parsedTargetId = Number(errorLogTargetId);
                  const targetId = Number.isFinite(parsedTargetId) && parsedTargetId > 0 ? parsedTargetId : null;
                  void loadErrorLog(targetId);
                }}
                disabled={errorLogLoading}
              >
                {errorLogLoading ? "Оновлюю..." : "Оновити"}
              </Button>
            </div>

            {errorLogError ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{errorLogError}</div>
            ) : null}

            <div className="min-h-0 flex-1 overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Час помилки</TableHead>
                    <TableHead>Ціль</TableHead>
                    <TableHead>Тип задачі</TableHead>
                    <TableHead>Акаунт</TableHead>
                    <TableHead>Статус</TableHead>
                    <TableHead>Спроба</TableHead>
                    <TableHead>Помилка</TableHead>
                    <TableHead>Дія</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {errorLogRows.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell>{row.error_at_text || "-"}</TableCell>
                      <TableCell>{row.target_name}</TableCell>
                      <TableCell>{row.job_type}</TableCell>
                      <TableCell>{row.account_label}</TableCell>
                      <TableCell>{row.status}</TableCell>
                      <TableCell>{row.attempt}</TableCell>
                      <TableCell className="max-w-[540px] whitespace-pre-wrap break-words text-xs">{row.error || "-"}</TableCell>
                      <TableCell>
                        <Button
                          size="sm"
                          variant="outline"
                          type="button"
                          disabled={retryingJobId === row.id}
                          onClick={() => void retryJob(row.id)}
                        >
                          {retryingJobId === row.id ? "Перезапуск..." : "Перезапустити"}
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                  {!errorLogLoading && errorLogRows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={8}>Помилок не знайдено.</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
