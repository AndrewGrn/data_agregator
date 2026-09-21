import { Pencil, Play, RefreshCw, Search, Square } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiGet, apiPost } from "../api";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Textarea } from "../components/ui/textarea";

type DarknetTarget = {
  id: number;
  name: string;
  identifier: string;
  config: Record<string, unknown>;
  is_active: boolean;
  running: number;
  queued: number;
  failed: number;
  events_count: number;
  last_success_text: string;
  process_text: string;
  adapter: string;
  login_required: boolean;
  start_urls_count: number;
  adapter_detected: string | null;
  adapter_suggested: string | null;
  adapter_detected_error: string | null;
};

type DarknetAccount = {
  id: number;
  label: string;
  is_active: boolean;
  utilization: number;
  queued_jobs: number;
  last_success_text: string;
  auth_mode: string;
  auth_state_text: string;
  username: string | null;
  proxy_url: string;
  state_cookies_count: number;
  state_origins_count: number;
};

type DarknetJob = {
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
};

type DarknetRecentEvent = {
  id: number;
  target_id: number;
  target_name: string;
  target_identifier: string;
  account_id: number | null;
  account_label: string;
  event_type: string;
  event_type_text: string;
  author: string | null;
  thread_url: string | null;
  thread_title: string | null;
  preview_text: string;
  external_id: string | null;
  observed_at: string | null;
  observed_at_text: string;
};

type DarknetModuleResponse = {
  summary: {
    targets: number;
    accounts: number;
    events_count: number;
    message_events_count: number;
    profile_events_count: number;
    service_events_count: number;
    problem_jobs: number;
  };
  targets: DarknetTarget[];
  accounts: DarknetAccount[];
  jobs: DarknetJob[];
};

export function DarknetPage() {
  const [data, setData] = useState<DarknetModuleResponse | null>(null);
  const [recentEvents, setRecentEvents] = useState<DarknetRecentEvent[]>([]);
  const [showServiceEvents, setShowServiceEvents] = useState(false);
  const [loading, setLoading] = useState(true);
  const [pendingTargetId, setPendingTargetId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [serviceLoading, setServiceLoading] = useState("");

  const [targetName, setTargetName] = useState("");
  const [targetIdentifier, setTargetIdentifier] = useState("");
  const [targetAdapter, setTargetAdapter] = useState("xenforo");
  const [targetLoginRequired, setTargetLoginRequired] = useState(false);
  const [targetStartUrls, setTargetStartUrls] = useState("");
  const [targetCollectMaximum, setTargetCollectMaximum] = useState(true);
  const [targetReparseEnabled, setTargetReparseEnabled] = useState(true);
  const [targetReparseMinutes, setTargetReparseMinutes] = useState("15");
  const [targetMaxThreads, setTargetMaxThreads] = useState("0");
  const [targetMaxPages, setTargetMaxPages] = useState("0");
  const [targetMaxPosts, setTargetMaxPosts] = useState("0");
  const [targetMaxDiscoverPages, setTargetMaxDiscoverPages] = useState("0");
  const [targetSubmitting, setTargetSubmitting] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editTargetId, setEditTargetId] = useState<number | null>(null);
  const [editTargetName, setEditTargetName] = useState("");
  const [editTargetIdentifier, setEditTargetIdentifier] = useState("");
  const [editTargetAdapter, setEditTargetAdapter] = useState("xenforo");
  const [editTargetStartUrls, setEditTargetStartUrls] = useState("");
  const [editTargetLoginRequired, setEditTargetLoginRequired] = useState(false);
  const [editTargetCollectMaximum, setEditTargetCollectMaximum] = useState(true);
  const [editTargetReparseEnabled, setEditTargetReparseEnabled] = useState(true);
  const [editTargetReparseMinutes, setEditTargetReparseMinutes] = useState("15");
  const [editTargetDiscoverIntervalSeconds, setEditTargetDiscoverIntervalSeconds] = useState("60");
  const [editTargetMaxThreads, setEditTargetMaxThreads] = useState("0");
  const [editTargetMaxPages, setEditTargetMaxPages] = useState("0");
  const [editTargetMaxPosts, setEditTargetMaxPosts] = useState("0");
  const [editTargetMaxDiscoverPages, setEditTargetMaxDiscoverPages] = useState("0");

  const [accountLabel, setAccountLabel] = useState("");
  const [accountAuthMode, setAccountAuthMode] = useState<"form_login" | "browser_state">("browser_state");
  const [accountUsername, setAccountUsername] = useState("");
  const [accountPassword, setAccountPassword] = useState("");
  const [accountStorageState, setAccountStorageState] = useState("");
  const [accountBrowserUserAgent, setAccountBrowserUserAgent] = useState("");
  const [accountProxyUrl, setAccountProxyUrl] = useState("default");
  const [accountFallbackFormLogin, setAccountFallbackFormLogin] = useState(false);
  const [accountLoginPagePath, setAccountLoginPagePath] = useState("/login/");
  const [accountLoginSubmitPath, setAccountLoginSubmitPath] = useState("/login/login");
  const [accountHourlyLimit, setAccountHourlyLimit] = useState("120");
  const [accountSubmitting, setAccountSubmitting] = useState(false);
  const [updateStateAccountId, setUpdateStateAccountId] = useState("");
  const [updateStateJson, setUpdateStateJson] = useState("");
  const [updateStateSubmitting, setUpdateStateSubmitting] = useState(false);
  const [helperAccountId, setHelperAccountId] = useState("");
  const [helperForumUrl, setHelperForumUrl] = useState("");
  const [helperStarting, setHelperStarting] = useState(false);
  const [helperToken, setHelperToken] = useState("");
  const [helperExpiresText, setHelperExpiresText] = useState("");
  const [helperCommand, setHelperCommand] = useState("");

  const [linkTargetId, setLinkTargetId] = useState("");
  const [linkAccountId, setLinkAccountId] = useState("");
  const [linkSubmitting, setLinkSubmitting] = useState(false);

  const load = useCallback(async () => {
    try {
      const [moduleResponse, recentResponse] = await Promise.all([
        apiGet<DarknetModuleResponse>("/api/modules/darknet"),
        apiGet<{ rows: DarknetRecentEvent[] }>(
          `/api/modules/darknet/events/recent?limit=60&include_service=${showServiceEvents ? "true" : "false"}`
        ),
      ]);
      setData(moduleResponse);
      setRecentEvents(recentResponse.rows ?? []);
      setError("");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити Darknet модуль");
      }
    } finally {
      setLoading(false);
    }
  }, [showServiceEvents]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      void load();
    }, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const toBool = (value: unknown, fallback: boolean): boolean => {
    if (typeof value === "boolean") {
      return value;
    }
    if (typeof value === "string") {
      const normalized = value.trim().toLowerCase();
      if (normalized === "true") {
        return true;
      }
      if (normalized === "false") {
        return false;
      }
    }
    if (typeof value === "number") {
      return value !== 0;
    }
    return fallback;
  };

  const toInt = (value: unknown, fallback: number, min = 0): number => {
    const parsed = Number.parseInt(String(value ?? ""), 10);
    if (!Number.isFinite(parsed) || Number.isNaN(parsed)) {
      return Math.max(fallback, min);
    }
    return Math.max(parsed, min);
  };

  const openEditTarget = (target: DarknetTarget) => {
    const cfg = target.config || {};
    const cfgStartUrls = cfg.start_urls;
    const startUrls =
      Array.isArray(cfgStartUrls) && cfgStartUrls.length > 0
        ? cfgStartUrls.map((item) => String(item ?? "").trim()).filter(Boolean)
        : [target.identifier];
    setEditTargetId(target.id);
    setEditTargetName(target.name || "");
    setEditTargetIdentifier(target.identifier || "");
    setEditTargetAdapter(target.adapter === "phpbb_like" ? "phpbb_like" : "xenforo");
    setEditTargetStartUrls(startUrls.join("\n"));
    setEditTargetLoginRequired(toBool(cfg.login_required, target.login_required));
    setEditTargetCollectMaximum(toBool(cfg.collect_maximum, true));
    setEditTargetReparseEnabled(toBool(cfg.reparse_existing_threads, true));
    setEditTargetReparseMinutes(String(toInt(cfg.thread_reparse_interval_minutes, 15, 1)));
    setEditTargetDiscoverIntervalSeconds(String(toInt(cfg.discover_interval_seconds, 60, 10)));
    setEditTargetMaxThreads(String(toInt(cfg.max_threads_per_cycle, 0, 0)));
    setEditTargetMaxPages(String(toInt(cfg.max_pages_per_thread, 0, 0)));
    setEditTargetMaxPosts(String(toInt(cfg.max_posts_per_thread, 0, 0)));
    setEditTargetMaxDiscoverPages(String(toInt(cfg.max_discover_pages_per_start, 0, 0)));
    setEditOpen(true);
  };

  const saveEditTarget = async () => {
    const targetId = Number(editTargetId);
    if (!targetId) {
      setError("Не вдалося визначити ціль для редагування");
      return;
    }
    if (!editTargetIdentifier.trim()) {
      setError("Вкажи URL або ідентифікатор цілі");
      return;
    }

    setEditSubmitting(true);
    setError("");
    setInfo("");
    try {
      const parseNonNegativeInt = (value: string, fallback: number) => {
        const parsed = Number.parseInt(String(value || "").trim(), 10);
        if (!Number.isFinite(parsed) || Number.isNaN(parsed)) {
          return fallback;
        }
        return Math.max(parsed, 0);
      };

      const startUrls = String(editTargetStartUrls || "")
        .split(/\n+/)
        .map((item) => item.trim())
        .filter(Boolean);

      await apiPost<{ ok: boolean }>(`/api/modules/darknet/targets/${targetId}/update`, {
        name: editTargetName.trim() || editTargetIdentifier.trim(),
        identifier: editTargetIdentifier.trim(),
        adapter: editTargetAdapter,
        start_urls: startUrls.length ? startUrls : [editTargetIdentifier.trim()],
        login_required: editTargetLoginRequired,
        collect_maximum: editTargetCollectMaximum,
        reparse_existing_threads: editTargetReparseEnabled,
        thread_reparse_interval_minutes: Math.max(Number(editTargetReparseMinutes) || 15, 1),
        discover_interval_seconds: Math.max(Number(editTargetDiscoverIntervalSeconds) || 60, 10),
        max_threads_per_cycle: parseNonNegativeInt(editTargetMaxThreads, 0),
        max_pages_per_thread: parseNonNegativeInt(editTargetMaxPages, 0),
        max_posts_per_thread: parseNonNegativeInt(editTargetMaxPosts, 0),
        max_discover_pages_per_start: parseNonNegativeInt(editTargetMaxDiscoverPages, 0),
      });

      setEditOpen(false);
      setEditTargetId(null);
      setInfo("Налаштування цілі оновлено");
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

  const callTargetAction = async (targetId: number, action: "run-now" | "stop" | "start") => {
    setPendingTargetId(targetId);
    setError("");
    try {
      await apiPost<{ ok: boolean }>(`/api/modules/darknet/targets/${targetId}/${action}`);
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

  const createTarget = async () => {
    if (!targetIdentifier.trim()) {
      setError("Вкажи URL або ідентифікатор цілі");
      return;
    }
    setTargetSubmitting(true);
    setError("");
    setInfo("");
    try {
      const parseNonNegativeInt = (value: string, fallback: number) => {
        const parsed = Number.parseInt(String(value || "").trim(), 10);
        if (!Number.isFinite(parsed) || Number.isNaN(parsed)) {
          return fallback;
        }
        return Math.max(parsed, 0);
      };
      const startUrls = String(targetStartUrls || "")
        .split(/\n+/)
        .map((item) => item.trim())
        .filter(Boolean);
      await apiPost<{ id: number }>("/api/targets", {
        parser_type: "darknet",
        name: targetName.trim() || targetIdentifier.trim(),
        identifier: targetIdentifier.trim(),
        config: {
          adapter: targetAdapter,
          start_urls: startUrls.length ? startUrls : [targetIdentifier.trim()],
          login_required: targetLoginRequired,
          collect_maximum: targetCollectMaximum,
          reparse_existing_threads: targetReparseEnabled,
          thread_reparse_interval_minutes: Number(targetReparseMinutes) || 15,
          max_threads_per_cycle: parseNonNegativeInt(targetMaxThreads, 0),
          max_pages_per_thread: parseNonNegativeInt(targetMaxPages, 0),
          max_posts_per_thread: parseNonNegativeInt(targetMaxPosts, 0),
          max_discover_pages_per_start: parseNonNegativeInt(targetMaxDiscoverPages, 0),
          thread_url_contains: targetAdapter === "phpbb_like" ? "/viewtopic.php" : "/threads/",
        },
      });
      setTargetName("");
      setTargetIdentifier("");
      setTargetStartUrls("");
      setTargetAdapter("xenforo");
      setTargetLoginRequired(false);
      setTargetCollectMaximum(true);
      setTargetReparseEnabled(true);
      setTargetReparseMinutes("15");
      setTargetMaxThreads("0");
      setTargetMaxPages("0");
      setTargetMaxPosts("0");
      setTargetMaxDiscoverPages("0");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити darknet-ціль");
      }
    } finally {
      setTargetSubmitting(false);
    }
  };

  const createAccount = async () => {
    if (!accountLabel.trim()) {
      setError("Вкажи мітку акаунта");
      return;
    }
    setAccountSubmitting(true);
    setError("");
    setInfo("");
    try {
      await apiPost<{ id: number; cookies_count: number }>("/api/modules/darknet/accounts", {
        label: accountLabel.trim(),
        hourly_limit: Number(accountHourlyLimit) || 120,
        auth_mode: accountAuthMode,
        username: accountUsername.trim(),
        password: accountPassword.trim(),
        login_page_path: accountLoginPagePath.trim() || "/login/",
        login_submit_path: accountLoginSubmitPath.trim() || "/login/login",
        storage_state_json: accountStorageState,
        browser_user_agent: accountBrowserUserAgent.trim(),
        proxy_url: accountProxyUrl.trim() || "default",
        fallback_form_login: accountFallbackFormLogin,
      });
      setAccountLabel("");
      setAccountAuthMode("browser_state");
      setAccountUsername("");
      setAccountPassword("");
      setAccountStorageState("");
      setAccountBrowserUserAgent("");
      setAccountProxyUrl("default");
      setAccountFallbackFormLogin(false);
      setAccountLoginPagePath("/login/");
      setAccountLoginSubmitPath("/login/login");
      setAccountHourlyLimit("120");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити darknet-акаунт");
      }
    } finally {
      setAccountSubmitting(false);
    }
  };

  const updateBrowserState = async () => {
    const accountId = Number(updateStateAccountId);
    if (!accountId) {
      setError("Обери акаунт для оновлення browser state");
      return;
    }
    if (!updateStateJson.trim()) {
      setError("Встав JSON storageState (cookies + origins)");
      return;
    }

    setUpdateStateSubmitting(true);
    setError("");
    setInfo("");
    try {
      const response = await apiPost<{ ok: boolean; cookies_count: number; origins_count: number }>(
        `/api/modules/darknet/accounts/${accountId}/browser-state`,
        {
          storage_state_json: updateStateJson,
          proxy_url: accountProxyUrl.trim() || "default",
          browser_user_agent: accountBrowserUserAgent.trim(),
          fallback_form_login: accountFallbackFormLogin,
        }
      );
      setInfo(`Browser state оновлено: cookies=${response.cookies_count}, origins=${response.origins_count}`);
      setUpdateStateJson("");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося оновити browser state");
      }
    } finally {
      setUpdateStateSubmitting(false);
    }
  };

  const startAuthHelper = async () => {
    const accountId = Number(helperAccountId);
    if (!accountId) {
      setError("Обери акаунт для helper авторизації");
      return;
    }
    setHelperStarting(true);
    setError("");
    setInfo("");
    try {
      const response = await apiPost<{
        ok: boolean;
        account_id: number;
        forum_url: string;
        expires_at_text: string;
        auth_token: string;
        helper_command: string;
      }>(`/api/modules/darknet/accounts/${accountId}/auth-helper/start`, {
        forum_url: helperForumUrl.trim(),
        api_base: window.location.origin,
      });
      setHelperToken(response.auth_token || "");
      setHelperExpiresText(response.expires_at_text || "-");
      setHelperCommand(response.helper_command || "");
      if (!helperForumUrl.trim() && response.forum_url) {
        setHelperForumUrl(response.forum_url);
      }
      setInfo(`Токен helper створено. Дійсний до ${response.expires_at_text}.`);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити helper-токен");
      }
      setHelperToken("");
      setHelperExpiresText("");
      setHelperCommand("");
    } finally {
      setHelperStarting(false);
    }
  };

  const createLink = async () => {
    const targetId = Number(linkTargetId);
    const accountId = Number(linkAccountId);
    if (!targetId || !accountId) {
      setError("Обери ціль і акаунт для прив'язки");
      return;
    }
    setLinkSubmitting(true);
    setError("");
    setInfo("");
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

  const runServiceAction = async (key: string, path: string, body: Record<string, unknown> | undefined = undefined) => {
    setServiceLoading(key);
    setError("");
    setInfo("");
    try {
      const response = await apiPost(path, body);
      if (response && typeof response === "object" && "updated" in (response as Record<string, unknown>)) {
        setInfo(`Оновлено записів: ${String((response as Record<string, unknown>).updated)}`);
      }
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

  const detectAdapter = async (targetId: number) => {
    setServiceLoading(`detect-${targetId}`);
    setError("");
    setInfo("");
    try {
      const response = await apiPost<{ ok: boolean; result: { recommended_adapter: string; detected: string | null } }>(
        `/api/modules/darknet/targets/${targetId}/detect-adapter`,
        { max_urls: 3 }
      );
      const detected = response.result.detected || "-";
      const recommended = response.result.recommended_adapter || "-";
      setInfo(`Автовизначення для цілі #${targetId}: detected=${detected}, recommended=${recommended}`);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося визначити адаптер");
      }
    } finally {
      setServiceLoading("");
    }
  };

  const changeAdapter = async (targetId: number, adapter: string) => {
    setServiceLoading(`adapter-${targetId}`);
    setError("");
    setInfo("");
    try {
      await apiPost<{ ok: boolean }>(`/api/modules/darknet/targets/${targetId}/adapter`, { adapter });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося змінити адаптер");
      }
    } finally {
      setServiceLoading("");
    }
  };

  const activeJobs = useMemo(() => data?.jobs.filter((job) => job.status !== "succeeded") ?? [], [data?.jobs]);

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Darknet модуль</CardTitle>
          <CardDescription>Керування парсингом форумів і ресурсів darknet.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
          {info ? <div className="rounded-md border border-primary/30 bg-primary/10 p-3 text-sm">{info}</div> : null}
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
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
              <p className="text-2xl font-semibold">{data?.summary.message_events_count ?? 0}</p>
            </div>
            <div className="rounded-md border bg-muted p-3">
              <p className="text-xs text-muted-foreground">Збережено профілів</p>
              <p className="text-2xl font-semibold">{data?.summary.profile_events_count ?? 0}</p>
            </div>
            <div className="rounded-md border bg-muted p-3">
              <p className="text-xs text-muted-foreground">Службові події</p>
              <p className="text-2xl font-semibold">{data?.summary.service_events_count ?? 0}</p>
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
          <CardTitle className="text-lg">Додати darknet-ціль</CardTitle>
          <CardDescription>Створи форум-ціль і налаштуй базовий адаптер.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="dn-target-name">Назва</Label>
              <Input id="dn-target-name" value={targetName} onChange={(event) => setTargetName(event.target.value)} placeholder="Forum target" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-target-id">URL / ідентифікатор</Label>
              <Input
                id="dn-target-id"
                value={targetIdentifier}
                onChange={(event) => setTargetIdentifier(event.target.value)}
                placeholder="http://exampleonion/forum/"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-target-adapter">Адаптер</Label>
              <select
                id="dn-target-adapter"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={targetAdapter}
                onChange={(event) => setTargetAdapter(event.target.value === "phpbb_like" ? "phpbb_like" : "xenforo")}
              >
                <option value="xenforo">xenforo</option>
                <option value="phpbb_like">phpbb_like</option>
              </select>
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="dn-target-start-urls">Start URLs (по одному в рядок)</Label>
            <Textarea
              id="dn-target-start-urls"
              rows={3}
              value={targetStartUrls}
              onChange={(event) => setTargetStartUrls(event.target.value)}
              placeholder="http://exampleonion/forum/\nhttp://exampleonion/forum/other/"
            />
          </div>
          <div className="grid gap-3 md:grid-cols-5">
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={targetLoginRequired}
                onChange={(event) => setTargetLoginRequired(event.target.checked)}
              />
              Потрібен логін
            </label>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={targetCollectMaximum}
                onChange={(event) => setTargetCollectMaximum(event.target.checked)}
              />
              Максимальний збір
            </label>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={targetReparseEnabled}
                onChange={(event) => setTargetReparseEnabled(event.target.checked)}
              />
              Репарс тем
            </label>
            <div className="space-y-1">
              <Label htmlFor="dn-reparse-min">Інтервал репарсу (хв)</Label>
              <Input
                id="dn-reparse-min"
                type="number"
                min={1}
                value={targetReparseMinutes}
                onChange={(event) => setTargetReparseMinutes(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-max-threads">Тем за цикл</Label>
              <Input
                id="dn-max-threads"
                type="number"
                min={0}
                value={targetMaxThreads}
                onChange={(event) => setTargetMaxThreads(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-max-pages">Сторінок/тема</Label>
              <Input
                id="dn-max-pages"
                type="number"
                min={0}
                value={targetMaxPages}
                onChange={(event) => setTargetMaxPages(event.target.value)}
              />
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="dn-max-posts">Постів/тема</Label>
              <Input
                id="dn-max-posts"
                type="number"
                min={0}
                value={targetMaxPosts}
                onChange={(event) => setTargetMaxPosts(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-max-discover-pages">Сторінок discovery/start URL</Label>
              <Input
                id="dn-max-discover-pages"
                type="number"
                min={0}
                value={targetMaxDiscoverPages}
                onChange={(event) => setTargetMaxDiscoverPages(event.target.value)}
              />
            </div>
            <div className="flex items-end justify-end">
              <Button type="button" onClick={() => void createTarget()} disabled={targetSubmitting}>
                {targetSubmitting ? "Створюю..." : "Додати ціль"}
              </Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">Для лімітів значення 0 означає без ліміту.</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Підключити darknet-акаунт</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-4">
            <div className="space-y-1">
              <Label htmlFor="dn-acc-label">Мітка</Label>
              <Input id="dn-acc-label" value={accountLabel} onChange={(event) => setAccountLabel(event.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-acc-auth-mode">Режим авторизації</Label>
              <select
                id="dn-acc-auth-mode"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={accountAuthMode}
                onChange={(event) => setAccountAuthMode(event.target.value === "form_login" ? "form_login" : "browser_state")}
              >
                <option value="browser_state">browser_state (cookies/localStorage)</option>
                <option value="form_login">form_login (login/password)</option>
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-acc-hourly">Ліміт/год</Label>
              <Input
                id="dn-acc-hourly"
                type="number"
                min={1}
                value={accountHourlyLimit}
                onChange={(event) => setAccountHourlyLimit(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-acc-proxy">Proxy URL</Label>
              <Input
                id="dn-acc-proxy"
                value={accountProxyUrl}
                onChange={(event) => setAccountProxyUrl(event.target.value)}
                placeholder="default | socks5://... | direct"
              />
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="dn-acc-user">Логін</Label>
              <Input id="dn-acc-user" value={accountUsername} onChange={(event) => setAccountUsername(event.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-acc-pass">Пароль</Label>
              <Input
                id="dn-acc-pass"
                type="password"
                value={accountPassword}
                onChange={(event) => setAccountPassword(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-acc-browser-ua">Browser User-Agent (опціонально)</Label>
              <Input
                id="dn-acc-browser-ua"
                value={accountBrowserUserAgent}
                onChange={(event) => setAccountBrowserUserAgent(event.target.value)}
                placeholder="Mozilla/5.0 ..."
              />
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="dn-acc-login-page">Login page path</Label>
              <Input
                id="dn-acc-login-page"
                value={accountLoginPagePath}
                onChange={(event) => setAccountLoginPagePath(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-acc-login-submit">Login submit path</Label>
              <Input
                id="dn-acc-login-submit"
                value={accountLoginSubmitPath}
                onChange={(event) => setAccountLoginSubmitPath(event.target.value)}
              />
            </div>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={accountFallbackFormLogin}
                onChange={(event) => setAccountFallbackFormLogin(event.target.checked)}
              />
              Fallback на form login
            </label>
          </div>
          <div className="space-y-1">
            <Label htmlFor="dn-acc-state-json">storageState JSON (cookies + origins)</Label>
            <Textarea
              id="dn-acc-state-json"
              rows={6}
              value={accountStorageState}
              onChange={(event) => setAccountStorageState(event.target.value)}
              placeholder='{"cookies":[...],"origins":[...]}'
            />
          </div>
          <div className="flex items-center justify-end">
            <Button type="button" variant="outline" onClick={() => void createAccount()} disabled={accountSubmitting}>
              {accountSubmitting ? "Підключаю..." : "Підключити акаунт"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Оновити browser state акаунта</CardTitle>
          <CardDescription>Встав Playwright storageState JSON, щоб оновити cookies/localStorage після ручного логіну з капчею.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="dn-update-state-account">Акаунт</Label>
              <select
                id="dn-update-state-account"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={updateStateAccountId}
                onChange={(event) => setUpdateStateAccountId(event.target.value)}
              >
                <option value="">Оберіть акаунт</option>
                {(data?.accounts || []).map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-update-state-proxy">Proxy URL</Label>
              <Input
                id="dn-update-state-proxy"
                value={accountProxyUrl}
                onChange={(event) => setAccountProxyUrl(event.target.value)}
                placeholder="default | socks5://... | direct"
              />
            </div>
            <label className="flex items-center gap-2 text-sm md:pt-7">
              <input
                type="checkbox"
                checked={accountFallbackFormLogin}
                onChange={(event) => setAccountFallbackFormLogin(event.target.checked)}
              />
              Fallback на form login
            </label>
          </div>
          <div className="space-y-1">
            <Label htmlFor="dn-update-state-json">storageState JSON</Label>
            <Textarea
              id="dn-update-state-json"
              rows={6}
              value={updateStateJson}
              onChange={(event) => setUpdateStateJson(event.target.value)}
              placeholder='{"cookies":[...],"origins":[...]}'
            />
          </div>
          <div className="flex items-center justify-end">
            <Button type="button" variant="outline" onClick={() => void updateBrowserState()} disabled={updateStateSubmitting}>
              {updateStateSubmitting ? "Оновлюю..." : "Оновити browser state"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Локальний helper авторизації</CardTitle>
          <CardDescription>
            Створи одноразовий токен, запусти команду в терміналі, пройди капчу у Playwright, helper сам завантажить storageState в акаунт.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="dn-helper-account">Акаунт</Label>
              <select
                id="dn-helper-account"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={helperAccountId}
                onChange={(event) => setHelperAccountId(event.target.value)}
              >
                <option value="">Оберіть акаунт</option>
                {(data?.accounts || []).map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1 md:col-span-2">
              <Label htmlFor="dn-helper-forum">URL форуму (опціонально)</Label>
              <Input
                id="dn-helper-forum"
                value={helperForumUrl}
                onChange={(event) => setHelperForumUrl(event.target.value)}
                placeholder="https://bhf.pro/"
              />
            </div>
          </div>
          <div className="flex items-center justify-end">
            <Button type="button" variant="outline" onClick={() => void startAuthHelper()} disabled={helperStarting}>
              {helperStarting ? "Створюю токен..." : "Створити helper-команду"}
            </Button>
          </div>
          {helperCommand ? (
            <div className="space-y-1">
              <div className="text-xs text-muted-foreground">Токен дійсний до: {helperExpiresText || "-"}</div>
              <div className="text-xs text-muted-foreground">Токен: {helperToken || "-"}</div>
              <Label htmlFor="dn-helper-command">Команда для запуску</Label>
              <Textarea id="dn-helper-command" rows={4} readOnly value={helperCommand} />
              <div className="text-xs text-muted-foreground">Після запуску команди закрий вікно Playwright - state завантажиться автоматично.</div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Прив'язати акаунт до цілі</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="dn-link-target">Ціль</Label>
              <select
                id="dn-link-target"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={linkTargetId}
                onChange={(event) => setLinkTargetId(event.target.value)}
              >
                <option value="">Оберіть ціль</option>
                {(data?.targets || []).map((target) => (
                  <option key={target.id} value={target.id}>
                    {target.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="dn-link-account">Акаунт</Label>
              <select
                id="dn-link-account"
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
            onClick={() => void runServiceAction("retry", "/api/modules/darknet/jobs/retry-failed")}
            disabled={serviceLoading !== ""}
          >
            {serviceLoading === "retry" ? "Повторюю..." : "Повторити помилки"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-lg">Список цілей</CardTitle>
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
                <TableHead>URL / ідентифікатор</TableHead>
                <TableHead>Адаптер</TableHead>
                <TableHead>Стан</TableHead>
                <TableHead>Процес</TableHead>
                <TableHead>Події</TableHead>
                <TableHead>Останній успіх</TableHead>
                <TableHead>Дії</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.targets.map((target) => (
                <TableRow key={target.id}>
                  <TableCell>{target.id}</TableCell>
                  <TableCell>{target.name}</TableCell>
                  <TableCell>{target.identifier}</TableCell>
                  <TableCell className="space-y-1">
                    <select
                      className="h-8 w-full rounded-md border bg-background px-2 text-xs"
                      value={target.adapter}
                      onChange={(event) => void changeAdapter(target.id, event.target.value)}
                      disabled={serviceLoading !== ""}
                    >
                      <option value="xenforo">xenforo</option>
                      <option value="phpbb_like">phpbb_like</option>
                    </select>
                    <p className="text-xs text-muted-foreground">
                      {target.login_required ? "Потрібен логін" : "Без логіну"} | Start URLs: {target.start_urls_count}
                    </p>
                    {target.adapter_suggested ? (
                      <p className="text-xs text-muted-foreground">
                        Детект: {target.adapter_detected || "-"} | Рекомендовано: {target.adapter_suggested}
                      </p>
                    ) : null}
                    {target.adapter_detected_error ? (
                      <p className="text-xs text-destructive">{target.adapter_detected_error}</p>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <Badge variant={target.is_active ? "secondary" : "outline"}>{target.is_active ? "Увімкнено" : "Вимкнено"}</Badge>
                  </TableCell>
                  <TableCell>{target.process_text}</TableCell>
                  <TableCell>{target.events_count}</TableCell>
                  <TableCell>{target.last_success_text}</TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Button
                        size="icon"
                        variant="outline"
                        type="button"
                        disabled={pendingTargetId === target.id || serviceLoading !== ""}
                        onClick={() => openEditTarget(target)}
                        title="Редагувати ціль"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        size="icon"
                        variant="secondary"
                        type="button"
                        disabled={pendingTargetId === target.id}
                        onClick={() => void callTargetAction(target.id, "run-now")}
                        title="Запустити парсинг зараз"
                      >
                        <RefreshCw className="h-4 w-4" />
                      </Button>
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
                        disabled={serviceLoading !== ""}
                        onClick={() => void detectAdapter(target.id)}
                        title="Автовизначити адаптер"
                      >
                        <Search className="h-4 w-4" />
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
                  <TableHead>Авторизація</TableHead>
                  <TableHead>Стан сесії</TableHead>
                  <TableHead>Proxy</TableHead>
                  <TableHead>Завантаженість</TableHead>
                  <TableHead>Черга</TableHead>
                  <TableHead>Останній успіх</TableHead>
                  <TableHead>Дії</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.accounts.map((account) => (
                  <TableRow key={account.id}>
                    <TableCell>{account.id}</TableCell>
                    <TableCell>
                      <div>{account.label}</div>
                      <div className="text-xs text-muted-foreground">{account.username || "-"}</div>
                    </TableCell>
                    <TableCell>{account.auth_mode}</TableCell>
                    <TableCell>
                      <div>{account.auth_state_text}</div>
                      <div className="text-xs text-muted-foreground">
                        cookies: {account.state_cookies_count} | origins: {account.state_origins_count}
                      </div>
                    </TableCell>
                    <TableCell>{account.proxy_url}</TableCell>
                    <TableCell>{account.utilization}%</TableCell>
                    <TableCell>{account.queued_jobs}</TableCell>
                    <TableCell>{account.last_success_text}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => setUpdateStateAccountId(String(account.id))}
                        >
                          Для state
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => setHelperAccountId(String(account.id))}
                        >
                          Для helper
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {data && data.accounts.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9}>Акаунти відсутні.</TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Активні та проблемні задачі</CardTitle>
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
                  <TableHead>Помилка</TableHead>
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
                    <TableCell>{job.error ?? "-"}</TableCell>
                  </TableRow>
                ))}
                {activeJobs.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={8}>Активних або проблемних задач немає.</TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle className="text-lg">Останні спаршені дані (лог)</CardTitle>
              <CardDescription>Останні збережені події з Darknet у реальному часі.</CardDescription>
            </div>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={showServiceEvents}
                  onChange={(event) => setShowServiceEvents(event.target.checked)}
                />
                Показати службові
              </label>
              <Button type="button" variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
                Оновити
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="max-h-[420px] overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ID</TableHead>
                    <TableHead>Тип</TableHead>
                    <TableHead>Ціль</TableHead>
                    <TableHead>Акаунт</TableHead>
                    <TableHead>Автор</TableHead>
                    <TableHead>Час</TableHead>
                    <TableHead>Дані</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {recentEvents.map((event) => (
                    <TableRow key={event.id}>
                      <TableCell>{event.id}</TableCell>
                      <TableCell>{event.event_type_text}</TableCell>
                      <TableCell>
                        <div>{event.target_name}</div>
                        <div className="text-xs text-muted-foreground">{event.target_identifier || "-"}</div>
                      </TableCell>
                      <TableCell>{event.account_label}</TableCell>
                      <TableCell>{event.author || "-"}</TableCell>
                      <TableCell>{event.observed_at_text}</TableCell>
                      <TableCell>
                        <div className="max-w-[680px] whitespace-pre-wrap break-words text-sm">{event.preview_text}</div>
                        {event.thread_title ? <div className="text-xs text-muted-foreground">Тема: {event.thread_title}</div> : null}
                        {event.thread_url ? <div className="text-xs text-muted-foreground">{event.thread_url}</div> : null}
                      </TableCell>
                    </TableRow>
                  ))}
                  {recentEvents.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={7}>Поки немає спаршених подій.</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </section>

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>Редагувати ціль</DialogTitle>
            <DialogDescription>Онови налаштування парсингу для darknet-цілі.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="dn-edit-target-name">Назва</Label>
                <Input id="dn-edit-target-name" value={editTargetName} onChange={(event) => setEditTargetName(event.target.value)} />
              </div>
              <div className="space-y-1 md:col-span-2">
                <Label htmlFor="dn-edit-target-identifier">URL / ідентифікатор</Label>
                <Input
                  id="dn-edit-target-identifier"
                  value={editTargetIdentifier}
                  onChange={(event) => setEditTargetIdentifier(event.target.value)}
                />
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="dn-edit-target-adapter">Адаптер</Label>
                <select
                  id="dn-edit-target-adapter"
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                  value={editTargetAdapter}
                  onChange={(event) => setEditTargetAdapter(event.target.value === "phpbb_like" ? "phpbb_like" : "xenforo")}
                >
                  <option value="xenforo">xenforo</option>
                  <option value="phpbb_like">phpbb_like</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="dn-edit-start-urls">Start URLs (по одному в рядок)</Label>
                <Textarea
                  id="dn-edit-start-urls"
                  rows={3}
                  value={editTargetStartUrls}
                  onChange={(event) => setEditTargetStartUrls(event.target.value)}
                />
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              <label className="flex items-center gap-2 text-sm md:pt-7">
                <input
                  type="checkbox"
                  checked={editTargetLoginRequired}
                  onChange={(event) => setEditTargetLoginRequired(event.target.checked)}
                />
                Потрібен логін
              </label>
              <label className="flex items-center gap-2 text-sm md:pt-7">
                <input
                  type="checkbox"
                  checked={editTargetCollectMaximum}
                  onChange={(event) => setEditTargetCollectMaximum(event.target.checked)}
                />
                Максимальний збір
              </label>
              <label className="flex items-center gap-2 text-sm md:pt-7">
                <input
                  type="checkbox"
                  checked={editTargetReparseEnabled}
                  onChange={(event) => setEditTargetReparseEnabled(event.target.checked)}
                />
                Репарс тем
              </label>
              <div className="space-y-1">
                <Label htmlFor="dn-edit-reparse-min">Інтервал репарсу (хв)</Label>
                <Input
                  id="dn-edit-reparse-min"
                  type="number"
                  min={1}
                  value={editTargetReparseMinutes}
                  onChange={(event) => setEditTargetReparseMinutes(event.target.value)}
                />
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-5">
              <div className="space-y-1">
                <Label htmlFor="dn-edit-discover-int">Інтервал discovery (сек)</Label>
                <Input
                  id="dn-edit-discover-int"
                  type="number"
                  min={10}
                  value={editTargetDiscoverIntervalSeconds}
                  onChange={(event) => setEditTargetDiscoverIntervalSeconds(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="dn-edit-max-threads">Тем за цикл</Label>
                <Input
                  id="dn-edit-max-threads"
                  type="number"
                  min={0}
                  value={editTargetMaxThreads}
                  onChange={(event) => setEditTargetMaxThreads(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="dn-edit-max-pages">Сторінок/тема</Label>
                <Input
                  id="dn-edit-max-pages"
                  type="number"
                  min={0}
                  value={editTargetMaxPages}
                  onChange={(event) => setEditTargetMaxPages(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="dn-edit-max-posts">Постів/тема</Label>
                <Input
                  id="dn-edit-max-posts"
                  type="number"
                  min={0}
                  value={editTargetMaxPosts}
                  onChange={(event) => setEditTargetMaxPosts(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="dn-edit-max-discover-pages">Сторінок discovery/start URL</Label>
                <Input
                  id="dn-edit-max-discover-pages"
                  type="number"
                  min={0}
                  value={editTargetMaxDiscoverPages}
                  onChange={(event) => setEditTargetMaxDiscoverPages(event.target.value)}
                />
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setEditOpen(false)} disabled={editSubmitting}>
              Скасувати
            </Button>
            <Button type="button" onClick={() => void saveEditTarget()} disabled={editSubmitting}>
              {editSubmitting ? "Зберігаю..." : "Зберегти"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
