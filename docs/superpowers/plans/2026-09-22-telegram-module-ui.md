# Telegram-модуль: SaaS-интерфейс Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Страница `/telegram` показывает каналы под мониторингом строками с иконками-действиями и подсказками; один инпут подключает канал; настройки модуля показывают аккаунты с живостью и режимом пула; карточка аккаунта — его каналы.

**Architecture:** Дизайн-система (токены, оболочка с боковой навигацией, `Tooltip`, строка списка) создаётся через скилл `ui-ux-pro-max:design` и ложится в `frontend/src/components/ui` и `components/layout`. Три страницы модуля собираются из этих примитивов и ходят в API из бэкенд-плана (`/api/modules/telegram/...`). Старая страница на 1798 строк удаляется; флоу подключения аккаунта по коду выносится в отдельный диалог без изменений логики.

**Tech Stack:** React 18, TypeScript, Vite, Tailwind CSS 4 (`@import "tailwindcss"`, токены — HSL-переменные в `:root`), Radix primitives, lucide-react, shadcn-стиль компонентов, `cn()` из `frontend/src/lib/utils.ts`. Тест-раннера во фронтенде нет: проверка — `npx tsc --noEmit`, `npm run build`, живой браузер через Chrome DevTools MCP.

**Spec:** `docs/superpowers/specs/2026-09-22-telegram-module-redesign-design.md` (§6)

**Prerequisite:** План `2026-09-22-telegram-onboarding-backend.md` выполнен: эндпоинты `/api/modules/telegram/{onboard, accounts/overview, accounts/{id}/check-alive, accounts/{id}/pool-mode, accounts/{id}/targets, targets/{id}/reassign, targets/{id}/retry-onboarding}` существуют; `GET /api/modules/telegram` отдаёт `targets[]` с `account`, `onboarding_step`, `onboarding_error`, `kind`.

## Global Constraints

- Единственная новая зависимость — `@radix-ui/react-tooltip`. Ничего другого не добавлять.
- Светлая минималистичная тема; `dark`-переменные можно оставить как есть, но новые компоненты не должны зависеть от тёмной темы.
- Все иконки — `lucide-react`. Каждая иконка-кнопка обязана иметь подсказку (`Tooltip`) и `aria-label`.
- Тексты интерфейса — украинский, как в остальном проекте.
- API-вызовы только через хелперы `frontend/src/api.ts` (`apiGet`, `apiPost`); новых HTTP-обёрток не заводить.
- Опрос состояния списка — каждые 5 с, **только** пока есть строки в `queued | resolving | joining`; иначе не опрашивать.
- Маршруты: `/telegram`, `/telegram/settings`, `/telegram/accounts/:id`. Обёртка `<Protected><Shell>…</Shell></Protected>` как у существующих маршрутов в `App.tsx`.
- После каждой задачи: `cd frontend && npx tsc --noEmit` — новых ошибок нет (одна известная старая: `ImportMeta.env` в `src/api.ts`).
- Браузерная проверка через Chrome DevTools MCP: сначала `take_snapshot`, действия — по `uid` из снимка; `take_screenshot` только для отчёта. Адрес — `http://aggredata.localhost` (не `127.0.0.1:5183` — cookie сессии привязана к этому хосту).

---

## File Structure

**Создаются (Task 1, через `/design`):**
- `frontend/src/components/layout/Shell.tsx` — оболочка: боковая навигация, заголовок страницы. Переезжает из `App.tsx`.
- `frontend/src/components/ui/tooltip.tsx` — обёртка над `@radix-ui/react-tooltip` (`TooltipProvider`, `Tooltip`, `TooltipTrigger`, `TooltipContent`).
- `frontend/src/components/ui/icon-button.tsx` — `IconButton({label, icon, onClick, disabled, variant})` = `Button size="icon" variant="ghost"` + `Tooltip` + `aria-label`.
- `frontend/src/components/ui/status-dot.tsx` — `StatusDot({tone: "ok"|"warn"|"bad"|"muted", label})`.
- `frontend/src/components/ui/status-pill.tsx` — `StatusPill({tone, children})`.
- `frontend/src/components/ui/data-row.tsx` — `DataRow({leading, primary, secondary, meta, actions})` — горизонтальная строка списка с фиксированными зонами.
- `frontend/src/components/ui/empty-state.tsx` — `EmptyState({icon, title, hint, action})`.

**Создаются (Tasks 2–5):**
- `frontend/src/api/telegram.ts` — типы `TelegramTargetRow`, `TelegramAccountRow` и функции к эндпоинтам.
- `frontend/src/components/telegram/OnboardBar.tsx`, `TargetRow.tsx`, `AccountRow.tsx`, `ConnectAccountDialog.tsx`.
- `frontend/src/pages/telegram/TargetsPage.tsx`, `SettingsPage.tsx`, `AccountPage.tsx`.

**Изменяются:** `frontend/src/App.tsx` (маршруты, `Shell` из layout), `frontend/src/styles.css` (токены), `frontend/package.json`.

**Удаляется (Task 6):** `frontend/src/pages/TelegramPage.tsx`.

---

### Task 1: Дизайн-система через `/design`

**Files:**
- Create: `frontend/src/components/layout/Shell.tsx`, `frontend/src/components/ui/{tooltip,icon-button,status-dot,status-pill,data-row,empty-state}.tsx`
- Modify: `frontend/src/styles.css`, `frontend/src/App.tsx` (использовать `Shell` из layout), `frontend/package.json`

**Interfaces:**
- Produces (контракт для Tasks 3–5 — имена и пропсы фиксированы, визуал свободен):

```tsx
// components/ui/tooltip.tsx — re-export Radix с базовой стилизацией
export { TooltipProvider, Tooltip, TooltipTrigger, TooltipContent };

// components/ui/icon-button.tsx
export function IconButton(props: {
  label: string;                 // текст подсказки и aria-label
  icon: React.ComponentType<{ className?: string }>;
  onClick?: () => void;
  disabled?: boolean;
  variant?: "ghost" | "outline";
  tone?: "default" | "danger";
}): JSX.Element;

// components/ui/status-dot.tsx
export function StatusDot(props: { tone: "ok" | "warn" | "bad" | "muted"; label?: string }): JSX.Element;

// components/ui/status-pill.tsx
export function StatusPill(props: { tone: "ok" | "warn" | "bad" | "muted" | "info"; children: React.ReactNode }): JSX.Element;

// components/ui/data-row.tsx
export function DataRow(props: {
  leading?: React.ReactNode;     // иконка типа / аватар
  primary: React.ReactNode;      // название
  secondary?: React.ReactNode;   // идентификатор серым
  meta?: React.ReactNode;        // чипы статуса, время, счётчики
  actions?: React.ReactNode;     // IconButton-ы
  onClick?: () => void;
}): JSX.Element;

// components/ui/empty-state.tsx
export function EmptyState(props: { icon: React.ComponentType<{ className?: string }>; title: string; hint?: string; action?: React.ReactNode }): JSX.Element;

// components/layout/Shell.tsx
export function Shell(props: { children: React.ReactNode }): JSX.Element;
// навигация: Дашборд /, Telegram /telegram, WhatsApp /whatsapp, Darknet /darknet, Дані /data (+ существующие пункты из App.tsx)
```

- [ ] **Step 1: Install the tooltip primitive**

Run: `cd /Users/andriihrenchyshen/data_agregator/frontend && npm install @radix-ui/react-tooltip@^1`
Expected: `package.json` получает зависимость; `package-lock.json` обновлён.

- [ ] **Step 2: Invoke the design skill**

Вызвать скилл `ui-ux-pro-max:design` со следующим брифом (передать дословно):

> Проект: SaaS-панель мониторинга Telegram/WhatsApp/darknet-источников. Стек: React + TypeScript + Tailwind CSS 4 + Radix + lucide, shadcn-стиль компонентов, токены — HSL-переменные в `frontend/src/styles.css` (`--background`, `--primary`, `--muted`, `--border`, `--radius`). Нужно: (1) светлая минималистичная тема — обновить значения токенов в `:root`, не менять их имена; (2) оболочка страницы `components/layout/Shell.tsx` с левой боковой навигацией (пункты и маршруты — как в `frontend/src/App.tsx` сейчас, включая выход из аккаунта) и областью контента с заголовком; (3) компоненты `components/ui`: `tooltip.tsx` над `@radix-ui/react-tooltip`, `icon-button.tsx`, `status-dot.tsx`, `status-pill.tsx`, `data-row.tsx`, `empty-state.tsx` — сигнатуры пропсов приложены и обязательны. Плотность — компактная строка списка ~48px, одна строка = один объект. Цветовые тона статусов: ok / warn / bad / muted / info. Никаких новых зависимостей кроме `@radix-ui/react-tooltip`. Использовать `cn()` из `frontend/src/lib/utils.ts` и существующий `Button` из `components/ui/button.tsx`.

Приложить к брифу блок сигнатур из **Interfaces** выше.

- [ ] **Step 3: Move Shell out of App.tsx**

В `frontend/src/App.tsx` удалить локальную `function Shell(...)` (строки ~31–104) и импортировать `import { Shell } from "./components/layout/Shell";`. Проверить, что `Protected` и все существующие маршруты работают без изменений.

- [ ] **Step 4: Wrap the app in TooltipProvider**

В `frontend/src/main.tsx` (или в корне `App`) обернуть дерево в `<TooltipProvider delayDuration={300}>…</TooltipProvider>` — один провайдер на приложение.

- [ ] **Step 5: Typecheck and build**

Run: `cd /Users/andriihrenchyshen/data_agregator/frontend && npx tsc --noEmit && npm run build`
Expected: только известная ошибка `ImportMeta.env` в `src/api.ts` (если `tsc` падает на ней — сборка Vite всё равно должна пройти); `dist/` собирается.

- [ ] **Step 6: Browser check**

Chrome DevTools MCP: `new_page` → `http://aggredata.localhost/` (войти как `admin`, если требуется) → `take_snapshot` → убедиться, что боковая навигация присутствует, пункты кликабельны (`click` по `uid` пункта Telegram → URL `/telegram`, старая страница пока рендерится). `take_screenshot` для отчёта.

- [ ] **Step 7: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/styles.css frontend/src/App.tsx frontend/src/main.tsx frontend/src/components/layout frontend/src/components/ui
git commit -m "feat(ui): дизайн-система — светлые токены, Shell с боковой навигацией, Tooltip, IconButton, DataRow"
```

---

### Task 2: Типы и API-клиент модуля

**Files:**
- Create: `frontend/src/api/telegram.ts`

**Interfaces:**
- Consumes: `apiGet`, `apiPost` из `frontend/src/api.ts`.
- Produces:

```ts
export type OnboardingStep = "idle" | "queued" | "resolving" | "joining" | "joined" | "failed";
export type TargetKind = "channel" | "group" | "private" | null;

export interface TelegramTargetRow {
  id: number; name: string; identifier: string; kind: TargetKind;
  is_active: boolean;
  onboarding_status: "ready" | "needs_account" | "blocked";
  onboarding_step: OnboardingStep; onboarding_error: string | null;
  account: { id: number; label: string; alive: boolean | null } | null;
  events_count: number; last_event_at: string | null;
}

export interface TelegramAccountRow {
  id: number; label: string; username: string | null;
  pool_mode: "shared" | "dedicated"; is_active: boolean;
  alive: boolean | null; last_checked_at: string | null; dead_reason: string | null;
  health_score: number; cooldown_until: string | null;
  targets_count: number; joins_today: number; join_daily_limit: number;
}

export function fetchTelegramModule(): Promise<{ targets: TelegramTargetRow[]; [k: string]: unknown }>;
export function onboardTelegramTarget(input: string, opts?: { account_id?: number; allow_join?: boolean }): Promise<TelegramTargetRow>;
export function fetchTelegramAccounts(): Promise<TelegramAccountRow[]>;
export function checkTelegramAccount(id: number): Promise<{ alive: boolean; error: string | null; checked_at: string }>;
export function setTelegramAccountPoolMode(id: number, pool_mode: "shared" | "dedicated"): Promise<TelegramAccountRow>;
export function fetchTelegramAccountTargets(id: number): Promise<TelegramTargetRow[]>;
export function reassignTelegramTarget(id: number, account_id?: number): Promise<TelegramTargetRow>;
export function retryTelegramOnboarding(id: number): Promise<TelegramTargetRow>;
export function updateTelegramTarget(id: number, body: Record<string, unknown>): Promise<unknown>; // POST /modules/telegram/targets/{id}/update — существующий
```

- [ ] **Step 1: Write the module**

Создать `frontend/src/api/telegram.ts`:

```ts
import { apiGet, apiPost } from "../api";

export type OnboardingStep = "idle" | "queued" | "resolving" | "joining" | "joined" | "failed";
export type TargetKind = "channel" | "group" | "private" | null;

export interface TelegramTargetRow {
  id: number;
  name: string;
  identifier: string;
  kind: TargetKind;
  is_active: boolean;
  onboarding_status: "ready" | "needs_account" | "blocked";
  onboarding_step: OnboardingStep;
  onboarding_error: string | null;
  account: { id: number; label: string; alive: boolean | null } | null;
  events_count: number;
  last_event_at: string | null;
}

export interface TelegramAccountRow {
  id: number;
  label: string;
  username: string | null;
  pool_mode: "shared" | "dedicated";
  is_active: boolean;
  alive: boolean | null;
  last_checked_at: string | null;
  dead_reason: string | null;
  health_score: number;
  cooldown_until: string | null;
  targets_count: number;
  joins_today: number;
  join_daily_limit: number;
}

const BASE = "/api/modules/telegram";

export function fetchTelegramModule() {
  return apiGet<{ targets: TelegramTargetRow[]; [k: string]: unknown }>(BASE);
}

export function onboardTelegramTarget(input: string, opts: { account_id?: number; allow_join?: boolean } = {}) {
  return apiPost<TelegramTargetRow>(`${BASE}/onboard`, { input, ...opts });
}

export function fetchTelegramAccounts() {
  return apiGet<TelegramAccountRow[]>(`${BASE}/accounts/overview`);
}

export function checkTelegramAccount(id: number) {
  return apiPost<{ alive: boolean; error: string | null; checked_at: string }>(`${BASE}/accounts/${id}/check-alive`);
}

export function setTelegramAccountPoolMode(id: number, pool_mode: "shared" | "dedicated") {
  return apiPost<TelegramAccountRow>(`${BASE}/accounts/${id}/pool-mode`, { pool_mode });
}

export function fetchTelegramAccountTargets(id: number) {
  return apiGet<TelegramTargetRow[]>(`${BASE}/accounts/${id}/targets`);
}

export function reassignTelegramTarget(id: number, account_id?: number) {
  return apiPost<TelegramTargetRow>(`${BASE}/targets/${id}/reassign`, account_id ? { account_id } : {});
}

export function retryTelegramOnboarding(id: number) {
  return apiPost<TelegramTargetRow>(`${BASE}/targets/${id}/retry-onboarding`);
}

export function updateTelegramTarget(id: number, body: Record<string, unknown>) {
  return apiPost<unknown>(`${BASE}/targets/${id}/update`, body);
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: без новых ошибок.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/telegram.ts
git commit -m "feat(ui): типы и API-клиент Telegram-модуля"
```

---

### Task 3: Страница `/telegram` — инпут и список каналов

**Files:**
- Create: `frontend/src/components/telegram/OnboardBar.tsx`, `frontend/src/components/telegram/TargetRow.tsx`, `frontend/src/pages/telegram/TargetsPage.tsx`
- Modify: `frontend/src/App.tsx` (маршрут `/telegram` → `TargetsPage`)

**Interfaces:**
- Consumes: Task 1 компоненты; Task 2 функции и типы.
- Produces: `TargetsPage`, переиспользуемый `TargetRow({row, onChanged})` (нужен Task 5).

- [ ] **Step 1: OnboardBar**

Создать `frontend/src/components/telegram/OnboardBar.tsx`:

```tsx
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
      <form
        className="flex items-center gap-2"
        onSubmit={(e) => { e.preventDefault(); void submit(); }}
      >
        <div className="relative flex-1">
          <Link2 className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-9"
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
```

- [ ] **Step 2: TargetRow**

Создать `frontend/src/components/telegram/TargetRow.tsx`:

```tsx
import { useNavigate } from "react-router-dom";
import { Hash, Users, MessageCircle, Pause, Play, RefreshCw, Repeat, Settings2, Trash2, RotateCcw } from "lucide-react";
import { DataRow } from "../ui/data-row";
import { IconButton } from "../ui/icon-button";
import { StatusDot } from "../ui/status-dot";
import { StatusPill } from "../ui/status-pill";
import {
  reassignTelegramTarget,
  retryTelegramOnboarding,
  updateTelegramTarget,
  type TelegramTargetRow,
} from "../../api/telegram";

const KIND_ICON = { channel: Hash, group: Users, private: MessageCircle } as const;

function relative(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "щойно";
  if (m < 60) return `${m} хв тому`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} год тому`;
  return `${Math.floor(h / 24)} д тому`;
}

export function statusOf(row: TelegramTargetRow): { tone: "ok" | "warn" | "bad" | "muted" | "info"; text: string; title?: string } {
  if (!row.is_active) return { tone: "muted", text: "Пауза" };
  switch (row.onboarding_step) {
    case "queued":
    case "resolving": return { tone: "info", text: "У черзі" };
    case "joining": return { tone: "info", text: "Вступає" };
    case "failed": return { tone: "bad", text: "Помилка", title: row.onboarding_error ?? undefined };
  }
  if (row.onboarding_status === "needs_account") return { tone: "warn", text: "Чекає акаунт" };
  if (row.onboarding_status === "blocked") return { tone: "warn", text: "Акаунти недоступні" };
  if (row.account && row.account.alive === false) return { tone: "bad", text: "Акаунт мертвий" };
  return { tone: "ok", text: "Моніториться" };
}

export function TargetRow({ row, onChanged }: { row: TelegramTargetRow; onChanged: () => void }) {
  const navigate = useNavigate();
  const Icon = row.kind ? KIND_ICON[row.kind] : Hash;
  const status = statusOf(row);
  const inProgress = ["queued", "resolving", "joining"].includes(row.onboarding_step);

  const act = async (fn: () => Promise<unknown>) => { await fn(); onChanged(); };

  return (
    <DataRow
      leading={<Icon className="h-4 w-4 text-muted-foreground" aria-hidden />}
      primary={<span className="font-medium">{row.name}</span>}
      secondary={<span className="text-muted-foreground">{row.identifier}</span>}
      meta={
        <>
          <span title={status.title}><StatusPill tone={status.tone}>{status.text}</StatusPill></span>
          {row.account ? (
            <span className="inline-flex items-center gap-1.5 text-sm">
              <StatusDot tone={row.account.alive === false ? "bad" : row.account.alive ? "ok" : "muted"} />
              {row.account.label}
            </span>
          ) : (
            <span className="text-sm text-muted-foreground">без акаунта</span>
          )}
          <span className="text-sm text-muted-foreground">{relative(row.last_event_at)}</span>
          <span className="text-sm tabular-nums text-muted-foreground">{row.events_count}</span>
        </>
      }
      actions={
        <>
          {row.onboarding_step === "failed" && (
            <IconButton label="Повторити підключення" icon={RotateCcw} onClick={() => act(() => retryTelegramOnboarding(row.id))} />
          )}
          <IconButton
            label={row.is_active ? "Пауза" : "Відновити"}
            icon={row.is_active ? Pause : Play}
            onClick={() => act(() => updateTelegramTarget(row.id, { is_active: !row.is_active }))}
            disabled={inProgress}
          />
          <IconButton label="Бекфіл зараз" icon={RefreshCw} onClick={() => act(() => updateTelegramTarget(row.id, { run_now: true }))} disabled={inProgress || !row.account} />
          <IconButton label="Переназначити акаунт" icon={Repeat} onClick={() => act(() => reassignTelegramTarget(row.id))} disabled={inProgress} />
          <IconButton label="Налаштування" icon={Settings2} onClick={() => navigate(`/telegram?edit=${row.id}`)} />
          <IconButton label="Видалити" icon={Trash2} tone="danger" onClick={() => { if (window.confirm(`Видалити ${row.name}?`)) void act(() => updateTelegramTarget(row.id, { delete: true })); }} />
        </>
      }
    />
  );
}
```

Проверить по `app/routers/api.py` (`POST /modules/telegram/targets/{id}/update`), какие поля тела реально принимаются для паузы, запуска и удаления — подставить фактические имена вместо `is_active` / `run_now` / `delete`, если они другие. Если удаление реализовано отдельным эндпоинтом — добавить функцию в `api/telegram.ts` и вызвать её.

- [ ] **Step 3: TargetsPage**

Создать `frontend/src/pages/telegram/TargetsPage.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Radio, Settings } from "lucide-react";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { OnboardBar } from "../../components/telegram/OnboardBar";
import { TargetRow } from "../../components/telegram/TargetRow";
import { fetchTelegramModule, type TelegramTargetRow } from "../../api/telegram";

const IN_PROGRESS = new Set(["queued", "resolving", "joining"]);

export function TargetsPage() {
  const [rows, setRows] = useState<TelegramTargetRow[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const data = await fetchTelegramModule();
    setRows(data.targets);
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const busy = useMemo(() => rows.some((r) => IN_PROGRESS.has(r.onboarding_step)), [rows]);
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => { void load(); }, 5000);
    return () => window.clearInterval(t);
  }, [busy, load]);

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 p-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Telegram</h1>
          <p className="text-sm text-muted-foreground">{rows.length} об'єктів під моніторингом</p>
        </div>
        <Button asChild variant="outline">
          <Link to="/telegram/settings"><Settings className="mr-2 h-4 w-4" />Налаштування модуля</Link>
        </Button>
      </header>

      <OnboardBar onQueued={(row) => setRows((prev) => [row, ...prev.filter((r) => r.id !== row.id)])} />

      {loading ? null : rows.length === 0 ? (
        <EmptyState icon={Radio} title="Поки нічого не моніториться" hint="Вставте посилання на канал вище — решту система зробить сама." />
      ) : (
        <div className="divide-y rounded-lg border bg-card">
          {rows.map((row) => <TargetRow key={row.id} row={row} onChanged={load} />)}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Route it**

В `frontend/src/App.tsx` заменить элемент маршрута `/telegram` на `<TargetsPage />` (импорт из `./pages/telegram/TargetsPage`). Старый `TelegramPage` пока оставить импортированным только если на него ссылаются другие маршруты; иначе импорт удалить.

- [ ] **Step 5: Typecheck and browser check**

Run: `cd frontend && npx tsc --noEmit`

Chrome DevTools MCP: `navigate_page` → `http://aggredata.localhost/telegram` → `take_snapshot`: инпут, кнопка «Підключити», строки существующих таргетов с чипом аккаунта и статусом. Ввести `@durov` в инпут (`fill` по `uid`), нажать «Підключити» → строка появляется со статусом «У черзі»; в течение 5–10 с опрос обновляет её (при работающем воркере статус сменится). `hover` на иконку действия → подсказка видна. `list_console_messages` — без ошибок.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/telegram/OnboardBar.tsx frontend/src/components/telegram/TargetRow.tsx frontend/src/pages/telegram/TargetsPage.tsx frontend/src/App.tsx
git commit -m "feat(ui): /telegram — інпут підключення та список каналів рядками"
```

---

### Task 4: Настройки модуля — аккаунты и задачи

**Files:**
- Create: `frontend/src/components/telegram/AccountRow.tsx`, `frontend/src/components/telegram/ConnectAccountDialog.tsx`, `frontend/src/pages/telegram/SettingsPage.tsx`
- Modify: `frontend/src/App.tsx` (маршрут `/telegram/settings`)

**Interfaces:**
- Consumes: Task 1, Task 2; существующие эндпоинты `POST /modules/telegram/accounts/start-auth`, `complete-auth`, `/modules/telegram/jobs/*` (вызовы и тела скопировать из `pages/TelegramPage.tsx` — карточки «Підключити Telegram-акаунт» и «Активні та проблемні задачі»).
- Produces: `AccountRow({row, onChanged})` (нужен Task 5), `ConnectAccountDialog({open, onOpenChange, onConnected})`.

- [ ] **Step 1: ConnectAccountDialog**

Вынести из `frontend/src/pages/TelegramPage.tsx` (карточка «Підключити Telegram-акаунт», ~строки 779–851) состояние и запросы `start-auth` / `complete-auth` в `frontend/src/components/telegram/ConnectAccountDialog.tsx` на базе `components/ui/dialog.tsx`. Логику запросов и поля формы (api_id, api_hash, телефон, код, пароль 2FA) перенести **без изменений**; меняется только контейнер (диалог вместо карточки). Пропсы: `open: boolean; onOpenChange(open: boolean): void; onConnected(): void`.

- [ ] **Step 2: AccountRow**

Создать `frontend/src/components/telegram/AccountRow.tsx`:

```tsx
import { useNavigate } from "react-router-dom";
import { Activity, ExternalLink, Power, Trash2, Users } from "lucide-react";
import { DataRow } from "../ui/data-row";
import { IconButton } from "../ui/icon-button";
import { StatusDot } from "../ui/status-dot";
import { StatusPill } from "../ui/status-pill";
import { checkTelegramAccount, setTelegramAccountPoolMode, type TelegramAccountRow } from "../../api/telegram";

function ago(iso: string | null) {
  if (!iso) return "не перевірявся";
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "перевірено щойно" : `перевірено ${m} хв тому`;
}

export function AccountRow({ row, onChanged }: { row: TelegramAccountRow; onChanged: () => void }) {
  const navigate = useNavigate();
  const tone = row.alive === false ? "bad" : row.alive ? "ok" : "muted";
  const isShared = row.pool_mode === "shared";

  return (
    <DataRow
      leading={<Users className="h-4 w-4 text-muted-foreground" aria-hidden />}
      primary={<span className="font-medium">{row.label}</span>}
      secondary={<span className="text-muted-foreground">{row.username ? `@${row.username}` : "—"}</span>}
      meta={
        <>
          <button
            type="button"
            className="text-sm underline-offset-2 hover:underline"
            title={isShared ? "Бере канали з черги автоматично" : "Тільки ручне призначення"}
            onClick={async () => { await setTelegramAccountPoolMode(row.id, isShared ? "dedicated" : "shared"); onChanged(); }}
          >
            <StatusPill tone={isShared ? "info" : "muted"}>{isShared ? "Пул" : "Приватний"}</StatusPill>
          </button>
          <span className="inline-flex items-center gap-1.5 text-sm" title={row.dead_reason ?? undefined}>
            <StatusDot tone={tone} />
            {ago(row.last_checked_at)}
          </span>
          <span className="text-sm text-muted-foreground">{row.targets_count} каналів</span>
          <span className="text-sm tabular-nums text-muted-foreground">вступів {row.joins_today}/{row.join_daily_limit}</span>
        </>
      }
      actions={
        <>
          <IconButton label="Перевірити зараз" icon={Activity} onClick={async () => { await checkTelegramAccount(row.id); onChanged(); }} />
          <IconButton label="Відкрити акаунт" icon={ExternalLink} onClick={() => navigate(`/telegram/accounts/${row.id}`)} />
          <IconButton label={row.is_active ? "Вимкнути" : "Увімкнути"} icon={Power} onClick={() => navigate(`/telegram/accounts/${row.id}`)} />
          <IconButton label="Видалити" icon={Trash2} tone="danger" onClick={() => navigate(`/telegram/accounts/${row.id}`)} />
        </>
      }
    />
  );
}
```

Для «Вимкнути/Видалити» найти существующие эндпоинты в `app/routers/api.py` (`grep -n "accounts/{account_id}" app/routers/api.py`) и подключить их вместо перехода на карточку; если эндпоинтов нет — оставить переход и отметить в отчёте.

- [ ] **Step 3: SettingsPage**

Создать `frontend/src/pages/telegram/SettingsPage.tsx`: заголовок «Налаштування Telegram» с кнопкой назад к `/telegram`; `Tabs` (из `components/ui/tabs.tsx`) с двумя вкладками:

- **Акаунти**: кнопка «Підключити акаунт» (открывает `ConnectAccountDialog`), список `AccountRow` из `fetchTelegramAccounts()`, `EmptyState` при пустом списке. Меню «···» (`Popover` из `components/ui/popover.tsx`) с двумя действиями из старых «Сервісних дій»: «Синхронізувати учасників» (`POST /api/telegram/sync-memberships`) и «Скинути cooldown» (`POST /api/modules/telegram/accounts/reset-cooldown`).
- **Задачі**: перенести содержимое карточки «Активні та проблемні задачі» из `TelegramPage.tsx` (~1360–1500) без изменения запросов (`/modules/telegram/jobs/error-log`, `retry-failed`, `jobs/{id}/...`).

Маршрут `/telegram/settings` в `App.tsx` — той же обёрткой, что и `/telegram`.

- [ ] **Step 4: Typecheck and browser check**

`npx tsc --noEmit`; в браузере: `/telegram/settings` → вкладка «Акаунти» показывает существующие аккаунты с точкой живости; клик по чипу «Пул» переключает на «Приватний» и обратно; «Перевірити зараз» обновляет время проверки; диалог подключения открывается и закрывается; вкладка «Задачі» рендерит список задач.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/telegram/AccountRow.tsx frontend/src/components/telegram/ConnectAccountDialog.tsx frontend/src/pages/telegram/SettingsPage.tsx frontend/src/App.tsx
git commit -m "feat(ui): /telegram/settings — акаунти з живістю та режимом пулу, задачі"
```

---

### Task 5: Карточка аккаунта

**Files:**
- Create: `frontend/src/pages/telegram/AccountPage.tsx`
- Modify: `frontend/src/App.tsx` (маршрут `/telegram/accounts/:id`)

**Interfaces:**
- Consumes: `TargetRow` (Task 3), `fetchTelegramAccounts`, `fetchTelegramAccountTargets`, `onboardTelegramTarget` (Task 2), `StatusDot`, `StatusPill`, `EmptyState`.

- [ ] **Step 1: AccountPage**

Создать `frontend/src/pages/telegram/AccountPage.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Link2 } from "lucide-react";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { EmptyState } from "../../components/ui/empty-state";
import { StatusDot } from "../../components/ui/status-dot";
import { StatusPill } from "../../components/ui/status-pill";
import { TargetRow } from "../../components/telegram/TargetRow";
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

  const load = useCallback(async () => {
    const [accounts, rows] = await Promise.all([fetchTelegramAccounts(), fetchTelegramAccountTargets(accountId)]);
    setAccount(accounts.find((a) => a.id === accountId) ?? null);
    setTargets(rows);
  }, [accountId]);

  useEffect(() => { void load(); }, [load]);

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
    <div className="mx-auto flex max-w-6xl flex-col gap-6 p-6">
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
```

- [ ] **Step 2: Route it**

В `App.tsx` добавить маршрут `/telegram/accounts/:id` → `<AccountPage />` той же обёрткой.

- [ ] **Step 3: Typecheck and browser check**

`npx tsc --noEmit`; в браузере: со страницы настроек «Відкрити акаунт» → карточка с заголовком, точкой живости и списком его каналов; для аккаунта в режиме «Приватний» видна форма назначения; ввод `@durov` + «Призначити» создаёт строку в списке со статусом «У черзі».

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/telegram/AccountPage.tsx frontend/src/App.tsx
git commit -m "feat(ui): картка акаунта — його канали, ручне призначення для приватних"
```

---

### Task 6: Снос старой страницы и финальная проверка

**Files:**
- Delete: `frontend/src/pages/TelegramPage.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/api.ts` (мёртвые типы/функции, если остались)

- [ ] **Step 1: Confirm nothing imports the old page**

Run: `cd frontend && grep -rn "TelegramPage" src/ | grep -v "pages/TelegramPage.tsx"`
Expected: пусто (если маршрут в `App.tsx` ещё ссылается — исправить в Task 3 было пропущено; починить сейчас).

- [ ] **Step 2: Delete and clean**

```bash
git rm frontend/src/pages/TelegramPage.tsx
```

Прогнать `npx tsc --noEmit`; удалить экспорты из `src/api.ts`, которые использовались только старой страницей и теперь не имеют потребителей (проверить каждый `grep -rn "<name>" src/`).

- [ ] **Step 3: Build**

Run: `cd frontend && npm run build`
Expected: сборка успешна, размер бандла не вырос заметно (старая страница 1798 строк ушла).

- [ ] **Step 4: Full browser pass**

Chrome DevTools MCP, `http://aggredata.localhost`:
1. `/telegram` — список, инпут, подсказки на иконках, статусы; `list_console_messages` без ошибок.
2. Подключить канал по `@username` → «У черзі» → (при запущенном воркере) «Вступає» → «Моніториться» с чипом аккаунта.
3. `/telegram/settings` — аккаунты, переключение пула, проверка живости, диалог подключения, вкладка задач.
4. `/telegram/accounts/:id` — каналы аккаунта; для приватного — форма назначения.
5. `emulate` ширина 390px — строки не ломают вёрстку, действия доступны (допустимо схлопывание в меню «···», если `/design` его предусмотрел).
6. `take_screenshot` каждой страницы для отчёта.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src
git commit -m "chore(ui): видалити стару сторінку Telegram, прибрати мертвий код"
```

---

## Self-Review

**Покрытие спеки §6:**

| Спека | Задача |
|---|---|
| Дизайн-система через `/design`: токены, оболочка, `Tooltip`, строка | Task 1 |
| Единственная новая зависимость `@radix-ui/react-tooltip` | Task 1 шаг 1 |
| `/telegram`: один инпут + список строк с колонками из таблицы спеки | Task 3 |
| Строка в `queued/joining` показывает прогресс; в `failed` — ошибка и «повторить» | Task 3 (`statusOf`, кнопка `RotateCcw`) |
| Иконки с подсказками: пауза · бэкфилл · переназначить · настройки · удалить | Task 3 |
| `/telegram/settings`: аккаунты с тумблером пула, живостью, нагрузкой, действиями; подключение аккаунта; вкладка задач; «сервисные действия» в меню | Task 4 |
| `/telegram/accounts/:id`: карточка, каналы, ручное назначение с чекбоксом вступления | Task 5 |
| Девять карточек уходят | Task 6 |

**Плейсхолдеры:** имена полей тела `updateTelegramTarget` (`is_active`/`run_now`/`delete`) и эндпоинты выключения/удаления аккаунта помечены как «сверить с `api.py`» с конкретной командой поиска — это проверка существующего контракта, не отложенная работа. Визуал внутри компонентов Task 1 намеренно делегирован `/design`; их **сигнатуры** зафиксированы и используются в Tasks 3–5 дословно.

**Согласованность имён:** `TargetRow({row, onChanged})` — Task 3 определяет, Task 5 использует. `AccountRow({row, onChanged})` — Task 4. Все функции `api/telegram.ts` (Task 2) вызываются с теми же именами в Tasks 3–5. `StatusPill.tone` включает `"info"` — используется в `statusOf` и в чипе пула.

**Проверка без тест-раннера:** каждая задача заканчивается `tsc` и живым браузером через Chrome DevTools MCP по адресу `aggredata.localhost`. Это и есть «тест» для UI здесь; добавлять vitest в этот план — вне спеки.
