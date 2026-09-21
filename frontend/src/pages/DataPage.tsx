import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, apiGet, apiPost } from "../api";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Textarea } from "../components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { cn } from "../lib/utils";

type TargetOverview = {
  id: number;
  name: string;
  identifier: string;
  events_count: number;
  last_event_text: string;
};

type MessageRow = {
  id: number;
  target_id: number;
  target_name?: string;
  target_identifier?: string;
  external_id: string | null;
  message_kind: string;
  is_comment: boolean;
  sender_label: string;
  sender_id: number | null;
  text: string;
  root_post_id: number | null;
  parent_message_id: number | null;
  observed_at_text: string;
};

type UserRow = {
  telegram_user_id: number;
  display_name: string;
  full_name: string | null;
  memberships_count: number;
  status_text: string;
  is_active_any: boolean;
  last_seen_text: string;
};

type TargetRiskHit = {
  target_id: number;
  target_name: string;
  target_identifier: string;
  risk_label: string | null;
  is_active: boolean;
  status: string;
  last_seen_text: string;
};

type TargetRiskRow = {
  telegram_user_id: number;
  display_name: string;
  username: string | null;
  full_name: string | null;
  last_seen_text: string;
  risky_targets_total: number;
  risky_targets: TargetRiskHit[];
};

type TargetRiskResponse = {
  target: {
    id: number;
    name: string;
    identifier: string;
  };
  checked_users: number;
  risky_users: number;
  risky_only: boolean;
  rows: TargetRiskRow[];
};

type GlobalUserRow = {
  telegram_user_id: number;
  display_name: string;
  username: string | null;
  full_name: string | null;
  targets_count: number;
  last_seen_text: string;
};

type ProfileMembershipRow = {
  target_id: number;
  target_name: string;
  target_identifier: string;
  status: string;
  is_active: boolean;
  last_seen_text: string;
};

type ProfileResponse = {
  profile: {
    telegram_user_id: number;
    display_name: string;
    username: string | null;
    full_name: string | null;
    is_bot: boolean;
    is_verified: boolean;
    is_deleted: boolean;
    last_seen_text: string;
  };
  memberships: ProfileMembershipRow[];
  messages: MessageRow[];
};

type DarknetSearchUserRow = {
  darknet_user_id: number;
  display_name: string;
  username: string | null;
  forum_host: string;
  targets_count: number;
  threads_count: number;
  posts_count: number;
  last_seen_text: string;
};

type DarknetProfileMembershipRow = {
  target_id: number;
  target_name: string;
  target_identifier: string;
  thread_url: string;
  thread_title: string | null;
  posts_count: number;
  status: string;
  is_active: boolean;
  last_seen_text: string;
};

type DarknetProfileMessageRow = {
  id: number;
  target_id: number;
  target_name: string;
  target_identifier: string;
  thread_url: string | null;
  thread_title: string | null;
  author: string | null;
  text: string;
  external_id: string | null;
  observed_at_text: string;
};

type DarknetProfileResponse = {
  profile: {
    darknet_user_id: number;
    display_name: string;
    username: string | null;
    forum_host: string;
    last_seen_text: string;
  };
  memberships: DarknetProfileMembershipRow[];
  messages: DarknetProfileMessageRow[];
};

type SearchStatus = {
  enabled: boolean;
  backend: string;
  indexed_events: number;
};

type SearchHit = {
  event_id: number;
  parser_type: string;
  target_id: number;
  target_name: string;
  target_identifier: string;
  account_id: number | null;
  external_id: string | null;
  event_type: string;
  is_comment: boolean;
  sender_id: number | null;
  sender_label: string;
  sender_username: string | null;
  text: string;
  snippet: string | null;
  observed_at: string | null;
  observed_at_text: string;
};

type CrossCheckTarget = {
  target_id: number;
  target_name: string;
  target_identifier: string;
  is_risky: boolean;
  risk_label: string | null;
  is_active?: boolean;
  status?: string;
  last_seen_text?: string;
  hits?: number;
};

type CrossCheckMatch = {
  telegram_user_id: number;
  display_name: string;
  username: string;
  full_name: string | null;
  targets_total: number;
  risky_targets_total: number;
  safe_targets_total: number;
  risky_targets: CrossCheckTarget[];
  other_targets: CrossCheckTarget[];
  mentions_total: number;
  mention_targets: CrossCheckTarget[];
  is_verified: boolean;
  is_bot: boolean;
  is_deleted: boolean;
  last_seen_text: string;
};

type CrossCheckMentionOnly = {
  username: string;
  mentions_total: number;
  mention_targets: CrossCheckTarget[];
};

type CrossCheckResponse = {
  extracted_usernames: string[];
  matched: CrossCheckMatch[];
  unresolved_usernames: string[];
  mention_only: CrossCheckMentionOnly[];
  search_unavailable: boolean;
};

type TargetIntelCandidate = {
  username: string;
  mentions: number;
  last_seen_text: string;
  sample_text: string;
};

type TargetIntelResponse = {
  target: {
    id: number;
    name: string;
    identifier: string;
  };
  scan_limit: number;
  full_scan: boolean;
  max_scan_events: number;
  truncated: boolean;
  scanned_events: number;
  extracted_usernames: string[];
  candidates: TargetIntelCandidate[];
  cross_check: CrossCheckResponse;
};

export function DataPage() {
  const [targets, setTargets] = useState<TargetOverview[]>([]);
  const [selectedTargetId, setSelectedTargetId] = useState<number | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [search, setSearch] = useState("");
  const [chatLimit, setChatLimit] = useState(100);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [usersLoading, setUsersLoading] = useState(false);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messagesLoaded, setMessagesLoaded] = useState(false);

  const [globalQuery, setGlobalQuery] = useState("");
  const [globalLimit, setGlobalLimit] = useState(50);
  const [globalSearching, setGlobalSearching] = useState(false);
  const [globalUsers, setGlobalUsers] = useState<GlobalUserRow[]>([]);
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileData, setProfileData] = useState<ProfileResponse | null>(null);
  const [profileError, setProfileError] = useState("");
  const [activeProfileUserId, setActiveProfileUserId] = useState<number | null>(null);
  const [darknetQuery, setDarknetQuery] = useState("");
  const [darknetLimit, setDarknetLimit] = useState(50);
  const [darknetSearching, setDarknetSearching] = useState(false);
  const [darknetUsers, setDarknetUsers] = useState<DarknetSearchUserRow[]>([]);
  const [darknetProfileLoading, setDarknetProfileLoading] = useState(false);
  const [darknetProfileData, setDarknetProfileData] = useState<DarknetProfileResponse | null>(null);
  const [darknetProfileError, setDarknetProfileError] = useState("");
  const [activeDarknetUserId, setActiveDarknetUserId] = useState<number | null>(null);
  const [darknetRebuildLoading, setDarknetRebuildLoading] = useState(false);
  const [darknetRebuildInfo, setDarknetRebuildInfo] = useState("");
  const [searchStatus, setSearchStatus] = useState<SearchStatus | null>(null);
  const [searchIndexError, setSearchIndexError] = useState("");
  const [globalTextQuery, setGlobalTextQuery] = useState("");
  const [globalTextParser, setGlobalTextParser] = useState<"all" | "telegram" | "darknet">("all");
  const [globalTextLimit, setGlobalTextLimit] = useState(50);
  const [globalTextLoading, setGlobalTextLoading] = useState(false);
  const [globalTextHits, setGlobalTextHits] = useState<SearchHit[]>([]);
  const [globalTextTotal, setGlobalTextTotal] = useState(0);
  const [crossInput, setCrossInput] = useState("");
  const [crossUsernamesInput, setCrossUsernamesInput] = useState("");
  const [crossLoading, setCrossLoading] = useState(false);
  const [crossError, setCrossError] = useState("");
  const [crossResult, setCrossResult] = useState<CrossCheckResponse | null>(null);
  const [targetIntelLoading, setTargetIntelLoading] = useState(false);
  const [targetIntelError, setTargetIntelError] = useState("");
  const [targetIntelResult, setTargetIntelResult] = useState<TargetIntelResponse | null>(null);
  const [targetIntelScanLimit, setTargetIntelScanLimit] = useState(20000);
  const [targetIntelFullScan, setTargetIntelFullScan] = useState(false);
  const [targetIntelMaxEvents, setTargetIntelMaxEvents] = useState(200000);
  const [targetIntelOnlyRisky, setTargetIntelOnlyRisky] = useState(false);
  const [crossOnlyRisky, setCrossOnlyRisky] = useState(false);
  const [targetRiskLoading, setTargetRiskLoading] = useState(false);
  const [targetRiskError, setTargetRiskError] = useState("");
  const [targetRiskOnly, setTargetRiskOnly] = useState(true);
  const [targetRiskResult, setTargetRiskResult] = useState<TargetRiskResponse | null>(null);

  const loadTargets = useCallback(async () => {
    try {
      const rows = await apiGet<TargetOverview[]>("/api/telegram/targets-overview");
      setTargets(rows);
      setSelectedTargetId((prev) => {
        if (rows.length === 0) {
          return null;
        }
        if (prev && rows.some((item) => item.id === prev)) {
          return prev;
        }
        return rows[0].id;
      });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити цілі");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTargets();
    const timer = setInterval(() => {
      void loadTargets();
    }, 12000);
    return () => clearInterval(timer);
  }, [loadTargets]);

  const loadSearchStatus = useCallback(async () => {
    try {
      const status = await apiGet<SearchStatus>("/api/search/status");
      setSearchStatus(status);
      setSearchIndexError("");
    } catch (err) {
      if (err instanceof ApiError) {
        setSearchIndexError(err.message);
      } else {
        setSearchIndexError("Не вдалося перевірити стан пошуку");
      }
    }
  }, []);

  useEffect(() => {
    void loadSearchStatus();
    const timer = setInterval(() => {
      void loadSearchStatus();
    }, 15000);
    return () => clearInterval(timer);
  }, [loadSearchStatus]);

  const loadTargetUsers = useCallback(async (targetId: number) => {
    setUsersLoading(true);
    setError("");
    try {
      const usersResponse = await apiGet<{ users: UserRow[] }>(`/api/telegram/targets/${targetId}/users?limit=500`);
      setUsers(usersResponse.users ?? []);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити користувачів цілі");
      }
    } finally {
      setUsersLoading(false);
    }
  }, []);

  const loadTargetMessages = useCallback(async () => {
    if (!selectedTargetId) {
      setMessages([]);
      setMessagesLoaded(false);
      return;
    }
    setMessagesLoading(true);
    setError("");
    try {
      const messagesResponse = await apiGet<{ messages: MessageRow[] }>(
        `/api/telegram/targets/${selectedTargetId}/messages?limit=${chatLimit}&q=${encodeURIComponent(search)}`
      );
      setMessages(messagesResponse.messages ?? []);
      setMessagesLoaded(true);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити повідомлення цілі");
      }
    } finally {
      setMessagesLoading(false);
    }
  }, [selectedTargetId, chatLimit, search]);

  const runTargetRiskCheck = useCallback(async (riskyOnly = targetRiskOnly) => {
    if (!selectedTargetId) {
      setTargetRiskError("Обери ціль");
      return;
    }
    setTargetRiskLoading(true);
    setTargetRiskError("");
    try {
      const response = await apiGet<TargetRiskResponse>(
        `/api/telegram/targets/${selectedTargetId}/risk-users?limit=5000&risky_only=${riskyOnly ? "true" : "false"}`
      );
      setTargetRiskResult(response);
    } catch (err) {
      if (err instanceof ApiError) {
        setTargetRiskError(err.message);
      } else {
        setTargetRiskError("Не вдалося перевірити акаунти цілі на ризикові перетини");
      }
      setTargetRiskResult(null);
    } finally {
      setTargetRiskLoading(false);
    }
  }, [selectedTargetId, targetRiskOnly]);

  useEffect(() => {
    if (!selectedTargetId) {
      setUsers([]);
      setMessages([]);
      setMessagesLoaded(false);
      return;
    }
    setMessages([]);
    setMessagesLoaded(false);
    void loadTargetUsers(selectedTargetId);
  }, [selectedTargetId, loadTargetUsers]);

  useEffect(() => {
    if (!selectedTargetId) {
      return;
    }
    const timer = setInterval(() => {
      void loadTargetUsers(selectedTargetId);
    }, 12000);
    return () => clearInterval(timer);
  }, [selectedTargetId, loadTargetUsers]);

  useEffect(() => {
    if (!messagesLoaded || !selectedTargetId) {
      return;
    }
    const timer = setInterval(() => {
      void loadTargetMessages();
    }, 12000);
    return () => clearInterval(timer);
  }, [messagesLoaded, selectedTargetId, loadTargetMessages]);

  const selectedTarget = useMemo(
    () => targets.find((target) => target.id === selectedTargetId) ?? null,
    [targets, selectedTargetId]
  );

  const targetIntelMatchedByUsername = useMemo(() => {
    const map = new Map<string, CrossCheckMatch>();
    for (const item of targetIntelResult?.cross_check.matched ?? []) {
      map.set(String(item.username || "").toLowerCase(), item);
    }
    return map;
  }, [targetIntelResult]);

  const targetIntelMentionOnlyByUsername = useMemo(() => {
    const map = new Map<string, CrossCheckMentionOnly>();
    for (const item of targetIntelResult?.cross_check.mention_only ?? []) {
      map.set(String(item.username || "").toLowerCase(), item);
    }
    return map;
  }, [targetIntelResult]);

  const filteredTargetIntelCandidates = useMemo(() => {
    const rows = targetIntelResult?.candidates ?? [];
    if (!targetIntelOnlyRisky) {
      return rows;
    }
    return rows.filter((item) => {
      const key = String(item.username || "").toLowerCase();
      const matched = targetIntelMatchedByUsername.get(key);
      if (matched && Number(matched.risky_targets_total || 0) > 0) {
        return true;
      }
      const mentionOnly = targetIntelMentionOnlyByUsername.get(key);
      return Boolean((mentionOnly?.mention_targets || []).some((target) => Boolean(target.is_risky)));
    });
  }, [targetIntelResult, targetIntelOnlyRisky, targetIntelMatchedByUsername, targetIntelMentionOnlyByUsername]);

  const filteredCrossMatched = useMemo(() => {
    const rows = crossResult?.matched ?? [];
    if (!crossOnlyRisky) {
      return rows;
    }
    return rows.filter((item) => Number(item.risky_targets_total || 0) > 0);
  }, [crossResult, crossOnlyRisky]);

  const searchGlobalUsers = useCallback(async (resetProfile = false) => {
    const q = globalQuery.trim();
    if (!q) {
      setGlobalUsers([]);
      setProfileData(null);
      setActiveProfileUserId(null);
      setProfileError("");
      return;
    }
    setGlobalSearching(true);
    setProfileError("");
    try {
      const response = await apiGet<{ users: GlobalUserRow[] }>(
        `/api/telegram/users/search?q=${encodeURIComponent(q)}&limit=${globalLimit}`
      );
      setGlobalUsers(response.users ?? []);
      if (resetProfile) {
        setProfileData(null);
        setActiveProfileUserId(null);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setProfileError(err.message);
      } else {
        setProfileError("Не вдалося виконати пошук профілів");
      }
    } finally {
      setGlobalSearching(false);
    }
  }, [globalQuery, globalLimit]);

  const loadProfile = useCallback(async (telegramUserId: number) => {
    setProfileLoading(true);
    setProfileError("");
    try {
      const response = await apiGet<ProfileResponse>(`/api/telegram/users/${telegramUserId}/profile?limit=200`);
      setProfileData(response);
      setActiveProfileUserId(telegramUserId);
    } catch (err) {
      if (err instanceof ApiError) {
        setProfileError(err.message);
      } else {
        setProfileError("Не вдалося завантажити профіль");
      }
      setProfileData(null);
    } finally {
      setProfileLoading(false);
    }
  }, []);

  const searchDarknetUsers = useCallback(async (resetProfile = false) => {
    const q = darknetQuery.trim();
    if (!q) {
      setDarknetUsers([]);
      if (resetProfile) {
        setDarknetProfileData(null);
        setActiveDarknetUserId(null);
      }
      setDarknetProfileError("");
      return;
    }
    setDarknetSearching(true);
    setDarknetProfileError("");
    try {
      const response = await apiGet<{ users: DarknetSearchUserRow[] }>(
        `/api/darknet/users/search?q=${encodeURIComponent(q)}&limit=${darknetLimit}`
      );
      setDarknetUsers(response.users ?? []);
      if (resetProfile) {
        setDarknetProfileData(null);
        setActiveDarknetUserId(null);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setDarknetProfileError(err.message);
      } else {
        setDarknetProfileError("Не вдалося виконати пошук darknet-профілів");
      }
    } finally {
      setDarknetSearching(false);
    }
  }, [darknetQuery, darknetLimit]);

  const loadDarknetProfile = useCallback(async (darknetUserId: number) => {
    setDarknetProfileLoading(true);
    setDarknetProfileError("");
    try {
      const response = await apiGet<DarknetProfileResponse>(`/api/darknet/users/${darknetUserId}/profile?limit=200`);
      setDarknetProfileData(response);
      setActiveDarknetUserId(darknetUserId);
    } catch (err) {
      if (err instanceof ApiError) {
        setDarknetProfileError(err.message);
      } else {
        setDarknetProfileError("Не вдалося завантажити darknet-профіль");
      }
      setDarknetProfileData(null);
    } finally {
      setDarknetProfileLoading(false);
    }
  }, []);

  const rebuildDarknetProfiles = useCallback(async () => {
    setDarknetRebuildLoading(true);
    setDarknetProfileError("");
    setDarknetRebuildInfo("");
    try {
      const response = await apiPost<{ ok: boolean; processed: number; upserted: number }>("/api/darknet/profiles/rebuild", {
        limit: 100000,
      });
      setDarknetRebuildInfo(`Rebuild завершено: оброблено ${response.processed}, оновлено ${response.upserted}`);
      if (darknetQuery.trim()) {
        await searchDarknetUsers(false);
      }
      if (activeDarknetUserId) {
        await loadDarknetProfile(activeDarknetUserId);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        setDarknetProfileError(err.message);
      } else {
        setDarknetProfileError("Не вдалося виконати rebuild darknet-профілів");
      }
    } finally {
      setDarknetRebuildLoading(false);
    }
  }, [darknetQuery, searchDarknetUsers, activeDarknetUserId, loadDarknetProfile]);

  const runGlobalTextSearch = useCallback(async (parserOverride?: "all" | "telegram" | "darknet") => {
    setGlobalTextLoading(true);
    setSearchIndexError("");
    try {
      const parserValue = parserOverride ?? globalTextParser;
      const parserParam = parserValue === "all" ? "" : `&parser_type=${parserValue}`;
      const response = await apiGet<{ hits: SearchHit[]; total: number }>(
        `/api/search/messages?q=${encodeURIComponent(globalTextQuery)}&limit=${globalTextLimit}${parserParam}`
      );
      setGlobalTextHits(response.hits ?? []);
      setGlobalTextTotal(Number(response.total ?? 0));
    } catch (err) {
      if (err instanceof ApiError) {
        setSearchIndexError(err.message);
      } else {
        setSearchIndexError("Не вдалося виконати пошук");
      }
      setGlobalTextHits([]);
      setGlobalTextTotal(0);
    } finally {
      setGlobalTextLoading(false);
    }
  }, [globalTextParser, globalTextQuery, globalTextLimit]);

  const runCrossCheck = useCallback(async () => {
    setCrossLoading(true);
    setCrossError("");
    try {
      const usernames = crossUsernamesInput
        .split(/[\s,;]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      const response = await apiPost<CrossCheckResponse>("/api/telegram/intel/cross-check", {
        text: crossInput,
        usernames,
      });
      setCrossResult(response);
    } catch (err) {
      if (err instanceof ApiError) {
        setCrossError(err.message);
      } else {
        setCrossError("Не вдалося виконати перехресну перевірку");
      }
      setCrossResult(null);
    } finally {
      setCrossLoading(false);
    }
  }, [crossInput, crossUsernamesInput]);

  const runTargetIntel = useCallback(async () => {
    if (!selectedTargetId) {
      setTargetIntelError("Обери ціль");
      return;
    }
    setTargetIntelLoading(true);
    setTargetIntelError("");
    try {
      const response = await apiPost<TargetIntelResponse>(`/api/telegram/targets/${selectedTargetId}/intel-extract`, {
        scan_limit: targetIntelScanLimit,
        full_scan: targetIntelFullScan,
        max_scan_events: targetIntelMaxEvents,
      });
      setTargetIntelResult(response);
    } catch (err) {
      if (err instanceof ApiError) {
        setTargetIntelError(err.message);
      } else {
        setTargetIntelError("Не вдалося автоматично витягти username");
      }
      setTargetIntelResult(null);
    } finally {
      setTargetIntelLoading(false);
    }
  }, [selectedTargetId, targetIntelScanLimit, targetIntelFullScan, targetIntelMaxEvents]);

  useEffect(() => {
    const q = globalQuery.trim();
    if (!q) {
      return;
    }
    const timer = setInterval(() => {
      void searchGlobalUsers(false);
    }, 20000);
    return () => clearInterval(timer);
  }, [globalQuery, searchGlobalUsers]);

  useEffect(() => {
    const q = darknetQuery.trim();
    if (!q) {
      return;
    }
    const timer = setInterval(() => {
      void searchDarknetUsers(false);
    }, 20000);
    return () => clearInterval(timer);
  }, [darknetQuery, searchDarknetUsers]);

  useEffect(() => {
    if (!activeProfileUserId) {
      return;
    }
    const timer = setInterval(() => {
      void loadProfile(activeProfileUserId);
    }, 20000);
    return () => clearInterval(timer);
  }, [activeProfileUserId, loadProfile]);

  useEffect(() => {
    if (!activeDarknetUserId) {
      return;
    }
    const timer = setInterval(() => {
      void loadDarknetProfile(activeDarknetUserId);
    }, 20000);
    return () => clearInterval(timer);
  }, [activeDarknetUserId, loadDarknetProfile]);

  useEffect(() => {
    setTargetIntelResult(null);
    setTargetIntelError("");
    setTargetRiskResult(null);
    setTargetRiskError("");
  }, [selectedTargetId]);

  useEffect(() => {
    if (!selectedTargetId) {
      return;
    }
    void runTargetIntel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTargetId]);

  useEffect(() => {
    if (!selectedTargetId || !targetRiskResult) {
      return;
    }
    const timer = setInterval(() => {
      void runTargetRiskCheck(targetRiskOnly);
    }, 20000);
    return () => clearInterval(timer);
  }, [selectedTargetId, targetRiskResult, targetRiskOnly, runTargetRiskCheck]);

  return (
    <>
      <Tabs defaultValue="telegram" className="space-y-4">
        <TabsList>
          <TabsTrigger value="telegram">Telegram</TabsTrigger>
          <TabsTrigger value="darknet">Darknet</TabsTrigger>
        </TabsList>
        <TabsContent value="telegram" className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Telegram дані</CardTitle>
          <CardDescription>Канали/чати, стрічка повідомлень і профілі користувачів.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="data-search">Пошук у стрічці обраної цілі</Label>
              <Input id="data-search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Текст, sender, id" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="data-limit">Ліміт стрічки</Label>
              <Input
                id="data-limit"
                value={chatLimit}
                type="number"
                min={20}
                max={1000}
                onChange={(e) => setChatLimit(Math.max(20, Math.min(1000, Number(e.target.value) || 100)))}
              />
            </div>
            <div className="flex items-end gap-2">
              <Button type="button" onClick={() => void loadTargetMessages()} disabled={!selectedTargetId || messagesLoading}>
                {messagesLoading ? "Завантаження..." : messagesLoaded ? "Оновити стрічку" : "Показати стрічку"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setMessages([]);
                  setMessagesLoaded(false);
                }}
                disabled={!messagesLoaded}
              >
                Очистити
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Глобальний текстовий пошук (Postgres FTS)</CardTitle>
          <CardDescription>
            Пошук по всіх проіндексованих повідомленнях Telegram/Darknet з урахуванням ваших прав доступу.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {searchIndexError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{searchIndexError}</div>
          ) : null}
          <div className="grid gap-3 md:grid-cols-[1fr_180px_120px_auto_auto]">
            <div className="space-y-1">
              <Label htmlFor="global-text-query">Запит</Label>
              <Input
                id="global-text-query"
                value={globalTextQuery}
                onChange={(event) => setGlobalTextQuery(event.target.value)}
                placeholder="Ключові слова, @username, ID..."
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="global-text-parser">Джерело</Label>
              <select
                id="global-text-parser"
                className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                value={globalTextParser}
                onChange={(event) => {
                  const value = event.target.value;
                  if (value === "telegram" || value === "darknet") {
                    setGlobalTextParser(value);
                  } else {
                    setGlobalTextParser("all");
                  }
                }}
              >
                <option value="all">Усі</option>
                <option value="telegram">Telegram</option>
                <option value="darknet">Darknet</option>
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="global-text-limit">Ліміт</Label>
              <Input
                id="global-text-limit"
                type="number"
                min={10}
                max={200}
                value={globalTextLimit}
                onChange={(event) => setGlobalTextLimit(Math.max(10, Math.min(200, Number(event.target.value) || 50)))}
              />
            </div>
            <div className="flex items-end">
              <Button type="button" onClick={() => void runGlobalTextSearch()} disabled={globalTextLoading}>
                {globalTextLoading ? "Пошук..." : "Знайти"}
              </Button>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <span>Пошук: {searchStatus?.enabled ? "увімкнено" : "вимкнено"}</span>
            <span>Backend: {searchStatus?.backend ?? "-"}</span>
            <span>Проіндексовано подій: {Number(searchStatus?.indexed_events ?? 0)}</span>
            <span>Знайдено: {globalTextTotal}</span>
          </div>
          <div className="max-h-[420px] overflow-auto pr-1">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Час</TableHead>
                  <TableHead>Джерело</TableHead>
                  <TableHead>Ціль</TableHead>
                  <TableHead>Автор</TableHead>
                  <TableHead>Фрагмент</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {globalTextHits.map((hit) => (
                  <TableRow key={`${hit.parser_type}:${hit.event_id}:${hit.external_id ?? "-"}`}>
                    <TableCell>{hit.observed_at_text}</TableCell>
                    <TableCell>
                      <div>{hit.parser_type}</div>
                      <div className="text-xs text-muted-foreground">{hit.event_type}</div>
                    </TableCell>
                    <TableCell>
                      <div>{hit.target_name}</div>
                      <div className="text-xs text-muted-foreground">{hit.target_identifier}</div>
                    </TableCell>
                    <TableCell>{hit.sender_label || "-"}</TableCell>
                    <TableCell>
                      <div className="whitespace-pre-wrap text-sm">{hit.snippet || hit.text || "-"}</div>
                    </TableCell>
                  </TableRow>
                ))}
                {globalTextHits.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5}>Немає результатів.</TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <section className="grid gap-4 xl:grid-cols-[280px_1fr_380px]">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Канали і чати</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="max-h-[68vh] space-y-2 overflow-auto pr-1">
              {targets.map((target) => (
                <Button
                  key={target.id}
                  type="button"
                  variant={selectedTargetId === target.id ? "secondary" : "ghost"}
                  className={cn(
                    "h-auto w-full justify-start rounded-md border p-3 text-left",
                    selectedTargetId === target.id ? "border-primary/40" : "border-border"
                  )}
                  onClick={() => setSelectedTargetId(target.id)}
                >
                  <div className="space-y-1">
                    <div className="font-medium">{target.name}</div>
                    <div className="text-xs text-muted-foreground">{target.identifier}</div>
                    <div className="text-xs text-muted-foreground">Подій: {target.events_count}</div>
                    <div className="text-xs text-muted-foreground">Остання: {target.last_event_text}</div>
                  </div>
                </Button>
              ))}
              {!loading && targets.length === 0 ? <div className="text-sm text-muted-foreground">Цілей поки немає.</div> : null}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {selectedTarget ? `Стрічка: ${selectedTarget.name} (${selectedTarget.identifier})` : "Стрічка повідомлень"}
            </CardTitle>
            {!messagesLoaded ? (
              <CardDescription>Стрічка не завантажується автоматично. Натисни «Показати стрічку».</CardDescription>
            ) : null}
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="max-h-[68vh] space-y-3 overflow-auto pr-1">
            {messagesLoaded ? (
              <>
                {messages.map((msg) => (
                  <article
                    key={msg.id}
                    className={cn("rounded-md border bg-muted p-3", msg.is_comment ? "border-orange-300 bg-orange-50" : "border-border")}
                  >
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <p className="font-medium">{msg.sender_label}</p>
                      <p className="text-xs text-muted-foreground">{msg.observed_at_text}</p>
                    </div>
                    <p className="whitespace-pre-wrap text-sm">{msg.text}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      <Badge variant="outline">{msg.message_kind}</Badge>
                      <span>ID події: {msg.id}</span>
                      <span>External: {msg.external_id ?? "-"}</span>
                      {msg.is_comment ? <span>Root: {msg.root_post_id ?? "-"} | Reply: {msg.parent_message_id ?? "-"}</span> : null}
                    </div>
                  </article>
                ))}
                {messages.length === 0 ? <div className="text-sm text-muted-foreground">Повідомлень не знайдено.</div> : null}
              </>
            ) : (
              <div className="text-sm text-muted-foreground">Натисни «Показати стрічку», щоб завантажити повідомлення обраної цілі.</div>
            )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Користувачі обраної цілі</CardTitle>
          </CardHeader>
          <CardContent>
            {usersLoading ? <div className="text-sm text-muted-foreground">Завантаження...</div> : null}
            <div className="max-h-[68vh] overflow-auto pr-1">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Користувач</TableHead>
                    <TableHead>Стан</TableHead>
                    <TableHead>Остання активність</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {users.map((item) => (
                    <TableRow key={item.telegram_user_id}>
                      <TableCell>
                        <div>{item.display_name}</div>
                        <div className="text-xs text-muted-foreground">{item.full_name ?? "-"}</div>
                        <div className="text-xs text-muted-foreground">ID: {item.telegram_user_id}</div>
                      </TableCell>
                      <TableCell>
                        <div>{item.status_text}</div>
                        <div className="text-xs text-muted-foreground">{item.is_active_any ? "Активний" : "Неактивний"}</div>
                        <div className="text-xs text-muted-foreground">Підключень акаунтів: {item.memberships_count}</div>
                      </TableCell>
                      <TableCell>{item.last_seen_text}</TableCell>
                    </TableRow>
                  ))}
                  {users.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={3}>Користувачів не знайдено.</TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </section>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Перевірка акаунтів цілі на ризики</CardTitle>
          <CardDescription>
            Перевіряє користувачів обраного чату/каналу і показує, хто ще присутній у ризикових цілях.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {targetRiskError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{targetRiskError}</div>
          ) : null}
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm text-muted-foreground">
              {selectedTarget ? `Поточна ціль: ${selectedTarget.name} (${selectedTarget.identifier})` : "Ціль не обрана"}
            </div>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
                <input
                  type="checkbox"
                  checked={targetRiskOnly}
                  onChange={(event) => {
                    const checked = event.target.checked;
                    setTargetRiskOnly(checked);
                    if (targetRiskResult) {
                      void runTargetRiskCheck(checked);
                    }
                  }}
                />
                Лише ризикові акаунти
              </label>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setTargetRiskResult(null);
                  setTargetRiskError("");
                }}
              >
                Очистити
              </Button>
              <Button type="button" onClick={() => void runTargetRiskCheck()} disabled={!selectedTargetId || targetRiskLoading}>
                {targetRiskLoading ? "Перевіряю..." : "Перевірити акаунти цілі"}
              </Button>
            </div>
          </div>
          {targetRiskResult ? (
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground">
                Перевірено акаунтів: {targetRiskResult.checked_users}. З ризиковими перетинами: {targetRiskResult.risky_users}. Показано:{" "}
                {targetRiskResult.rows.length}.
              </div>
              <div className="max-h-[420px] overflow-auto pr-1">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Акаунт</TableHead>
                      <TableHead>Ризикові цілі</TableHead>
                      <TableHead>Деталі</TableHead>
                      <TableHead>Остання активність</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {targetRiskResult.rows.map((item) => (
                      <TableRow key={`target-risk-${item.telegram_user_id}`}>
                        <TableCell>
                          <div>{item.display_name}</div>
                          <div className="text-xs text-muted-foreground">ID: {item.telegram_user_id}</div>
                          <div className="text-xs text-muted-foreground">{item.full_name ?? "-"}</div>
                        </TableCell>
                        <TableCell>
                          <div className={item.risky_targets_total > 0 ? "font-semibold text-destructive" : ""}>
                            {item.risky_targets_total}
                          </div>
                        </TableCell>
                        <TableCell>
                          <div className="space-y-1 text-xs">
                            {item.risky_targets.slice(0, 3).map((target) => (
                              <div key={`target-risk-hit-${item.telegram_user_id}-${target.target_id}`}>
                                {target.target_name} ({target.target_identifier})
                                {target.risk_label ? ` [${target.risk_label}]` : ""}
                              </div>
                            ))}
                            {item.risky_targets.length > 3 ? <div>+{item.risky_targets.length - 3} ще</div> : null}
                          </div>
                        </TableCell>
                        <TableCell>{item.last_seen_text}</TableCell>
                      </TableRow>
                    ))}
                    {targetRiskResult.rows.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={4}>
                          {targetRiskOnly ? "Ризикових акаунтів у цій цілі не знайдено." : "Акаунтів для перевірки не знайдено."}
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Автовитяг нікнеймів з оголошень цілі</CardTitle>
          <CardDescription>
            Автоматично знаходить @username у повідомленнях обраного каналу/чату і відразу перевіряє перетини з ризиковими цілями.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {targetIntelError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{targetIntelError}</div>
          ) : null}
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm text-muted-foreground">
              {selectedTarget ? `Поточна ціль: ${selectedTarget.name} (${selectedTarget.identifier})` : "Ціль не обрана"}
            </div>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={200}
                max={20000}
                className="w-[120px]"
                value={targetIntelScanLimit}
                onChange={(event) => setTargetIntelScanLimit(Math.max(200, Math.min(20000, Number(event.target.value) || 20000)))}
                title="Розмір батча сканування"
              />
              <Input
                type="number"
                min={1000}
                max={1000000}
                className="w-[140px]"
                value={targetIntelMaxEvents}
                onChange={(event) => setTargetIntelMaxEvents(Math.max(1000, Math.min(1000000, Number(event.target.value) || 200000)))}
                title="Максимум подій для режиму повної історії"
                disabled={!targetIntelFullScan}
              />
              <label className="flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
                <input
                  type="checkbox"
                  checked={targetIntelFullScan}
                  onChange={(event) => setTargetIntelFullScan(event.target.checked)}
                />
                Уся історія
              </label>
              <label className="flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
                <input
                  type="checkbox"
                  checked={targetIntelOnlyRisky}
                  onChange={(event) => setTargetIntelOnlyRisky(event.target.checked)}
                />
                Лише ризикові
              </label>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setTargetIntelResult(null);
                  setTargetIntelError("");
                }}
              >
                Очистити
              </Button>
              <Button type="button" onClick={() => void runTargetIntel()} disabled={!selectedTargetId || targetIntelLoading}>
                {targetIntelLoading ? "Аналізую..." : "Знайти ніки автоматично"}
              </Button>
            </div>
          </div>

          {targetIntelResult ? (
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground">
                Переглянуто повідомлень: {targetIntelResult.scanned_events}. Знайдено username: {targetIntelResult.extracted_usernames.length}. Режим:{" "}
                {targetIntelResult.full_scan ? "вся історія" : "останні повідомлення"}.
                {targetIntelResult.truncated ? " Досягнуто ліміт сканування, збільш max events." : ""}
                {targetIntelOnlyRisky ? ` Показано ризикових: ${filteredTargetIntelCandidates.length}.` : ""}
              </div>
              <div className="max-h-[420px] overflow-auto pr-1">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Username</TableHead>
                      <TableHead>Згадок</TableHead>
                      <TableHead>Ризикові перетини</TableHead>
                      <TableHead>Останнє</TableHead>
                      <TableHead>Приклад</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filteredTargetIntelCandidates.map((item) => {
                      const key = String(item.username || "").toLowerCase();
                      const matched = targetIntelMatchedByUsername.get(key);
                      const mentionOnly = targetIntelMentionOnlyByUsername.get(key);
                      const riskyTotal = matched ? Number(matched.risky_targets_total || 0) : 0;
                      const totalTargets = matched ? Number(matched.targets_total || 0) : 0;
                      const mentionTargets = mentionOnly ? Number(mentionOnly.mention_targets.length || 0) : 0;
                      return (
                        <TableRow key={`intel-${item.username}`}>
                          <TableCell className="font-medium">{item.username}</TableCell>
                          <TableCell>{item.mentions}</TableCell>
                          <TableCell>
                            {matched ? (
                              <div className={riskyTotal > 0 ? "font-semibold text-destructive" : ""}>
                                {riskyTotal} ризик / {totalTargets} всього
                              </div>
                            ) : mentionOnly ? (
                              <div className="text-xs text-muted-foreground">Лише згадки у {mentionTargets} цілях</div>
                            ) : (
                              <div className="text-xs text-muted-foreground">Не знайдено профіль</div>
                            )}
                          </TableCell>
                          <TableCell>{item.last_seen_text}</TableCell>
                          <TableCell className="max-w-[420px]">
                            <div className="line-clamp-2 text-xs text-muted-foreground">{item.sample_text || "-"}</div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                    {filteredTargetIntelCandidates.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={5}>
                          {targetIntelOnlyRisky ? "Ризикових акаунтів не знайдено." : "У текстах цілі @username не знайдені."}
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Глобальний пошук профілю Telegram</CardTitle>
          <CardDescription>Пошук по всіх доступних цілях: де користувач присутній і що писав.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {profileError ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{profileError}</div> : null}
          <div className="grid gap-3 md:grid-cols-[1fr_160px_auto_auto]">
            <div className="space-y-1">
              <Label htmlFor="global-user-q">Пошук (username / id / ім'я)</Label>
              <Input
                id="global-user-q"
                value={globalQuery}
                onChange={(event) => setGlobalQuery(event.target.value)}
                placeholder="@username або 123456789"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="global-user-limit">Ліміт</Label>
              <Input
                id="global-user-limit"
                value={globalLimit}
                type="number"
                min={5}
                max={200}
                onChange={(event) => setGlobalLimit(Math.max(5, Math.min(200, Number(event.target.value) || 50)))}
              />
            </div>
            <div className="flex items-end">
              <Button type="button" onClick={() => void searchGlobalUsers(true)} disabled={globalSearching}>
                {globalSearching ? "Шукаю..." : "Знайти"}
              </Button>
            </div>
            <div className="flex items-end">
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setGlobalUsers([]);
                  setProfileData(null);
                  setActiveProfileUserId(null);
                  setProfileError("");
                }}
              >
                Очистити
              </Button>
            </div>
          </div>

          <div className="max-h-[380px] overflow-auto pr-1">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Профіль</TableHead>
                  <TableHead>Цілей</TableHead>
                  <TableHead>Остання активність</TableHead>
                  <TableHead>Дія</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {globalUsers.map((row) => (
                  <TableRow key={row.telegram_user_id}>
                    <TableCell>
                      <div>{row.display_name}</div>
                      <div className="text-xs text-muted-foreground">{row.full_name ?? "-"}</div>
                      <div className="text-xs text-muted-foreground">ID: {row.telegram_user_id}</div>
                    </TableCell>
                    <TableCell>{row.targets_count}</TableCell>
                    <TableCell>{row.last_seen_text}</TableCell>
                    <TableCell>
                      <Button type="button" size="sm" variant="outline" onClick={() => void loadProfile(row.telegram_user_id)} disabled={profileLoading}>
                        {profileLoading ? "Відкриваю..." : "Відкрити профіль"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
                {globalUsers.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4}>Профілі не знайдено.</TableCell>
                  </TableRow>
                ) : null}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Перехресна перевірка акаунтів</CardTitle>
          <CardDescription>
            Встав текст оголошення або @username. Система покаже, чи є ці акаунти в інших ваших цілях, зокрема в ризикових.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {crossError ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{crossError}</div> : null}
          <div className="grid gap-3 md:grid-cols-[1fr_300px]">
            <div className="space-y-1">
              <Label htmlFor="cross-input">Текст для аналізу</Label>
              <Textarea
                id="cross-input"
                rows={6}
                value={crossInput}
                onChange={(event) => setCrossInput(event.target.value)}
                placeholder="Встав повідомлення або опис, де є @username"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="cross-usernames">Додаткові username</Label>
              <Input
                id="cross-usernames"
                value={crossUsernamesInput}
                onChange={(event) => setCrossUsernamesInput(event.target.value)}
                placeholder="@driver5530 @another_user"
              />
            </div>
          </div>
          <div className="flex items-center justify-end gap-2">
            <label className="mr-auto flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
              <input
                type="checkbox"
                checked={crossOnlyRisky}
                onChange={(event) => setCrossOnlyRisky(event.target.checked)}
              />
              Лише ризикові акаунти
            </label>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setCrossInput("");
                setCrossUsernamesInput("");
                setCrossResult(null);
                setCrossError("");
              }}
            >
              Очистити
            </Button>
            <Button type="button" onClick={() => void runCrossCheck()} disabled={crossLoading}>
              {crossLoading ? "Перевіряю..." : "Перевірити перетини"}
            </Button>
          </div>

          {crossResult ? (
            <div className="space-y-3">
              {crossResult.search_unavailable ? (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
                  Пошук тимчасово недоступний: показано тільки перетини за профілями/memberships.
                </div>
              ) : null}
              <div className="text-xs text-muted-foreground">
                Знайдені username: {crossResult.extracted_usernames.length > 0 ? crossResult.extracted_usernames.join(", ") : "-"}
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Акаунт</TableHead>
                    <TableHead>Ризикові цілі</TableHead>
                    <TableHead>Усі цілі</TableHead>
                    <TableHead>Деталі</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredCrossMatched.map((item) => (
                    <TableRow key={`${item.telegram_user_id}:${item.username}`}>
                      <TableCell>
                        <div>{item.display_name}</div>
                        <div className="text-xs text-muted-foreground">ID: {item.telegram_user_id}</div>
                        <div className="text-xs text-muted-foreground">Остання активність: {item.last_seen_text}</div>
                      </TableCell>
                      <TableCell>
                        <div className={item.risky_targets_total > 0 ? "font-semibold text-destructive" : ""}>{item.risky_targets_total}</div>
                      </TableCell>
                      <TableCell>
                        <div>{item.targets_total}</div>
                        <div className="text-xs text-muted-foreground">Згадок у тексті: {item.mentions_total}</div>
                      </TableCell>
                      <TableCell>
                        <div className="space-y-1 text-xs">
                          {item.risky_targets.slice(0, 3).map((target) => (
                            <div key={`risk-${item.telegram_user_id}-${target.target_id}`}>
                              {target.target_name} ({target.target_identifier})
                            </div>
                          ))}
                          {item.risky_targets.length > 3 ? <div>+{item.risky_targets.length - 3} ще</div> : null}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                  {filteredCrossMatched.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={4}>
                        {crossOnlyRisky ? "Ризикових перетинів по профілях не знайдено." : "Перетинів по профілях не знайдено."}
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>

              {crossResult.unresolved_usernames.length > 0 ? (
                <div className="text-xs text-muted-foreground">
                  Не знайдено профіль у базі: {crossResult.unresolved_usernames.join(", ")}
                </div>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {profileData ? (
        <section className="grid gap-4 xl:grid-cols-[380px_1fr]">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{profileData.profile.display_name}</CardTitle>
              <CardDescription>
                {profileData.profile.full_name ?? "-"} | Last seen: {profileData.profile.last_seen_text}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <div>ID: {profileData.profile.telegram_user_id}</div>
              <div>Username: {profileData.profile.username ?? "-"}</div>
              <div>Bot: {profileData.profile.is_bot ? "так" : "ні"}</div>
              <div>Verified: {profileData.profile.is_verified ? "так" : "ні"}</div>
              <div>Deleted: {profileData.profile.is_deleted ? "так" : "ні"}</div>
              <div className="pt-2 text-xs font-medium text-muted-foreground">Де бере участь</div>
              <div className="max-h-[360px] overflow-auto pr-1">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Ціль</TableHead>
                      <TableHead>Стан</TableHead>
                      <TableHead>Остання</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {profileData.memberships.map((m) => (
                      <TableRow key={`${m.target_id}:${m.status}:${m.last_seen_text}`}>
                        <TableCell>
                          <div>{m.target_name}</div>
                          <div className="text-xs text-muted-foreground">{m.target_identifier}</div>
                        </TableCell>
                        <TableCell>
                          <div>{m.status}</div>
                          <div className="text-xs text-muted-foreground">{m.is_active ? "активний" : "неактивний"}</div>
                        </TableCell>
                        <TableCell>{m.last_seen_text}</TableCell>
                      </TableRow>
                    ))}
                    {profileData.memberships.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={3}>Немає даних про участь.</TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Що писав</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="max-h-[68vh] space-y-3 overflow-auto pr-1">
                {profileData.messages.map((msg) => (
                  <article
                    key={`profile-msg-${msg.id}`}
                    className={cn("rounded-md border bg-muted p-3", msg.is_comment ? "border-orange-300 bg-orange-50" : "border-border")}
                  >
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <p className="font-medium">{msg.target_name} <span className="text-xs text-muted-foreground">({msg.target_identifier})</span></p>
                      <p className="text-xs text-muted-foreground">{msg.observed_at_text}</p>
                    </div>
                    <p className="whitespace-pre-wrap text-sm">{msg.text}</p>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      <Badge variant="outline">{msg.message_kind}</Badge>
                      <span>ID події: {msg.id}</span>
                      <span>External: {msg.external_id ?? "-"}</span>
                      {msg.is_comment ? <span>Root: {msg.root_post_id ?? "-"} | Reply: {msg.parent_message_id ?? "-"}</span> : null}
                    </div>
                  </article>
                ))}
                {profileData.messages.length === 0 ? <div className="text-sm text-muted-foreground">Повідомлень для цього профілю не знайдено.</div> : null}
              </div>
            </CardContent>
          </Card>
        </section>
      ) : null}

        </TabsContent>

        <TabsContent value="darknet" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Текстовий пошук у повідомленнях Darknet</CardTitle>
              <CardDescription>Пошук по проіндексованих текстах постів форумів.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {searchIndexError ? (
                <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{searchIndexError}</div>
              ) : null}
              <div className="grid gap-3 md:grid-cols-[1fr_160px_auto]">
                <div className="space-y-1">
                  <Label htmlFor="darknet-text-query">Запит</Label>
                  <Input
                    id="darknet-text-query"
                    value={globalTextQuery}
                    onChange={(event) => setGlobalTextQuery(event.target.value)}
                    placeholder="Ключові слова, @username, телефон, ID..."
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="darknet-text-limit">Ліміт</Label>
                  <Input
                    id="darknet-text-limit"
                    type="number"
                    min={10}
                    max={200}
                    value={globalTextLimit}
                    onChange={(event) => setGlobalTextLimit(Math.max(10, Math.min(200, Number(event.target.value) || 50)))}
                  />
                </div>
                <div className="flex items-end">
                  <Button
                    type="button"
                    onClick={() => {
                      setGlobalTextParser("darknet");
                      void runGlobalTextSearch("darknet");
                    }}
                    disabled={globalTextLoading}
                  >
                    {globalTextLoading ? "Пошук..." : "Знайти у Darknet"}
                  </Button>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                <span>Пошук: {searchStatus?.enabled ? "увімкнено" : "вимкнено"}</span>
                <span>Проіндексовано подій: {Number(searchStatus?.indexed_events ?? 0)}</span>
                <span>Знайдено: {globalTextTotal}</span>
              </div>
              <div className="max-h-[320px] overflow-auto pr-1">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Час</TableHead>
                      <TableHead>Ціль</TableHead>
                      <TableHead>Автор</TableHead>
                      <TableHead>Фрагмент</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {globalTextHits.filter((hit) => hit.parser_type === "darknet").map((hit) => (
                      <TableRow key={`dn-text-${hit.event_id}-${hit.external_id ?? "-"}`}>
                        <TableCell>{hit.observed_at_text}</TableCell>
                        <TableCell>
                          <div>{hit.target_name}</div>
                          <div className="text-xs text-muted-foreground">{hit.target_identifier}</div>
                        </TableCell>
                        <TableCell>{hit.sender_label || "-"}</TableCell>
                        <TableCell>
                          <div className="whitespace-pre-wrap text-sm">{hit.snippet || hit.text || "-"}</div>
                        </TableCell>
                      </TableRow>
                    ))}
                    {globalTextHits.filter((hit) => hit.parser_type === "darknet").length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={4}>Немає результатів текстового пошуку.</TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Глобальний пошук профілю Darknet</CardTitle>
              <CardDescription>Пошук форумних профілів та участі у темах/форумах.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {darknetProfileError ? (
                <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{darknetProfileError}</div>
              ) : null}
              {darknetRebuildInfo ? <div className="rounded-md border border-primary/30 bg-primary/10 p-3 text-sm">{darknetRebuildInfo}</div> : null}
              <div className="grid gap-3 md:grid-cols-[1fr_160px_auto_auto_auto]">
                <div className="space-y-1">
                  <Label htmlFor="darknet-user-q">Пошук (username / host / id)</Label>
                  <Input
                    id="darknet-user-q"
                    value={darknetQuery}
                    onChange={(event) => setDarknetQuery(event.target.value)}
                    placeholder="@username або bhf.pro"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="darknet-user-limit">Ліміт</Label>
                  <Input
                    id="darknet-user-limit"
                    value={darknetLimit}
                    type="number"
                    min={5}
                    max={200}
                    onChange={(event) => setDarknetLimit(Math.max(5, Math.min(200, Number(event.target.value) || 50)))}
                  />
                </div>
                <div className="flex items-end">
                  <Button type="button" onClick={() => void searchDarknetUsers(true)} disabled={darknetSearching}>
                    {darknetSearching ? "Шукаю..." : "Знайти"}
                  </Button>
                </div>
                <div className="flex items-end">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => void rebuildDarknetProfiles()}
                    disabled={darknetRebuildLoading}
                  >
                    {darknetRebuildLoading ? "Оновлюю..." : "Rebuild профілів"}
                  </Button>
                </div>
                <div className="flex items-end">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setDarknetUsers([]);
                      setDarknetProfileData(null);
                      setActiveDarknetUserId(null);
                      setDarknetProfileError("");
                      setDarknetRebuildInfo("");
                    }}
                  >
                    Очистити
                  </Button>
                </div>
              </div>

              <div className="max-h-[380px] overflow-auto pr-1">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Профіль</TableHead>
                      <TableHead>Forum</TableHead>
                      <TableHead>Цілі/теми</TableHead>
                      <TableHead>Остання активність</TableHead>
                      <TableHead>Дія</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {darknetUsers.map((row) => (
                      <TableRow key={`darknet-user-${row.darknet_user_id}`}>
                        <TableCell>
                          <div>{row.display_name}</div>
                          <div className="text-xs text-muted-foreground">ID: {row.darknet_user_id}</div>
                          <div className="text-xs text-muted-foreground">{row.username ?? "-"}</div>
                        </TableCell>
                        <TableCell>{row.forum_host}</TableCell>
                        <TableCell>
                          <div>Цілей: {row.targets_count}</div>
                          <div className="text-xs text-muted-foreground">Тем: {row.threads_count} | Постів: {row.posts_count}</div>
                        </TableCell>
                        <TableCell>{row.last_seen_text}</TableCell>
                        <TableCell>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => void loadDarknetProfile(row.darknet_user_id)}
                            disabled={darknetProfileLoading}
                          >
                            {darknetProfileLoading ? "Відкриваю..." : "Відкрити профіль"}
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                    {darknetUsers.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={5}>Darknet-профілі не знайдено.</TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          {darknetProfileData ? (
            <section className="grid gap-4 xl:grid-cols-[420px_1fr]">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">{darknetProfileData.profile.display_name}</CardTitle>
                  <CardDescription>
                    {darknetProfileData.profile.forum_host} | Last seen: {darknetProfileData.profile.last_seen_text}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <div>ID: {darknetProfileData.profile.darknet_user_id}</div>
                  <div>Username: {darknetProfileData.profile.username ?? "-"}</div>
                  <div className="pt-2 text-xs font-medium text-muted-foreground">Де помічений</div>
                  <div className="max-h-[360px] overflow-auto pr-1">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Ціль/Тема</TableHead>
                          <TableHead>Статус</TableHead>
                          <TableHead>Остання</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {darknetProfileData.memberships.map((m, idx) => (
                          <TableRow key={`dn-membership-${m.target_id}-${idx}`}>
                            <TableCell>
                              <div>{m.target_name}</div>
                              <div className="text-xs text-muted-foreground">{m.target_identifier}</div>
                              <div className="text-xs text-muted-foreground break-all">{m.thread_title || m.thread_url}</div>
                            </TableCell>
                            <TableCell>
                              <div>{m.status}</div>
                              <div className="text-xs text-muted-foreground">{m.is_active ? "активний" : "неактивний"}</div>
                              <div className="text-xs text-muted-foreground">Постів: {m.posts_count}</div>
                            </TableCell>
                            <TableCell>{m.last_seen_text}</TableCell>
                          </TableRow>
                        ))}
                        {darknetProfileData.memberships.length === 0 ? (
                          <TableRow>
                            <TableCell colSpan={3}>Немає даних про участь.</TableCell>
                          </TableRow>
                        ) : null}
                      </TableBody>
                    </Table>
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Що писав на форумах</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="max-h-[68vh] space-y-3 overflow-auto pr-1">
                    {darknetProfileData.messages.map((msg) => (
                      <article key={`dn-profile-msg-${msg.id}`} className="rounded-md border bg-muted p-3">
                        <div className="mb-2 flex items-center justify-between gap-3">
                          <p className="font-medium">
                            {msg.target_name} <span className="text-xs text-muted-foreground">({msg.target_identifier})</span>
                          </p>
                          <p className="text-xs text-muted-foreground">{msg.observed_at_text}</p>
                        </div>
                        <p className="whitespace-pre-wrap text-sm">{msg.text}</p>
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                          <span>Автор: {msg.author ?? "-"}</span>
                          <span>ID події: {msg.id}</span>
                          <span>External: {msg.external_id ?? "-"}</span>
                        </div>
                        {msg.thread_title || msg.thread_url ? (
                          <div className="mt-1 text-xs text-muted-foreground break-all">{msg.thread_title || msg.thread_url}</div>
                        ) : null}
                      </article>
                    ))}
                    {darknetProfileData.messages.length === 0 ? (
                      <div className="text-sm text-muted-foreground">Повідомлень для цього darknet-профілю не знайдено.</div>
                    ) : null}
                  </div>
                </CardContent>
              </Card>
            </section>
          ) : null}
        </TabsContent>
      </Tabs>
    </>
  );
}
