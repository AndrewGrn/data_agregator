import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  type AdminRegistrationToken,
  type AdminResources,
  type AdminUserRow,
  ApiError,
  adminCreateRegistrationToken,
  adminDeactivateRegistrationToken,
  adminGetResources,
  adminListRegistrationTokens,
  adminListUsers,
  adminResetUserPassword,
  adminRevokeUserSessions,
  adminUpdateUser
} from "../api";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";

type UserDraft = {
  username: string;
  full_name: string;
  email: string;
};

export function AdminPage() {
  const [tokens, setTokens] = useState<AdminRegistrationToken[]>([]);
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [resources, setResources] = useState<AdminResources | null>(null);
  const [drafts, setDrafts] = useState<Record<number, UserDraft>>({});

  const [label, setLabel] = useState("");
  const [maxUses, setMaxUses] = useState(1);
  const [expiresInHours, setExpiresInHours] = useState(24);
  const [issuedToken, setIssuedToken] = useState("");
  const [passwordResetResult, setPasswordResetResult] = useState("");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [resetDialogOpen, setResetDialogOpen] = useState(false);
  const [resetUserId, setResetUserId] = useState<number | null>(null);
  const [resetPasswordValue, setResetPasswordValue] = useState("");
  const [resetLoading, setResetLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [tokenRows, userRows, resourceRows] = await Promise.all([
        adminListRegistrationTokens(),
        adminListUsers(),
        adminGetResources()
      ]);
      setTokens(tokenRows);
      setUsers(userRows);
      setResources(resourceRows);
      setDrafts((prev) => {
        const next: Record<number, UserDraft> = { ...prev };
        for (const row of userRows) {
          next[row.id] = next[row.id] ?? {
            username: row.username,
            full_name: row.full_name ?? "",
            email: row.email ?? ""
          };
        }
        return next;
      });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити адмін-панель");
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

  const usersById = useMemo(() => new Map(users.map((row) => [row.id, row])), [users]);
  const resetUser = resetUserId ? usersById.get(resetUserId) : null;

  const handleCreateToken = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setPasswordResetResult("");
    try {
      const result = await adminCreateRegistrationToken(label, maxUses, expiresInHours);
      setIssuedToken(result.token);
      setLabel("");
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося створити токен");
      }
    }
  };

  const handleDeactivateToken = async (tokenId: number) => {
    setError("");
    try {
      await adminDeactivateRegistrationToken(tokenId);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося деактивувати токен");
      }
    }
  };

  const handleUserRoleChange = async (userId: number, role: "admin" | "user") => {
    setError("");
    try {
      await adminUpdateUser(userId, { role });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося оновити роль");
      }
    }
  };

  const handleUserActiveToggle = async (row: AdminUserRow) => {
    setError("");
    try {
      await adminUpdateUser(row.id, { is_active: !row.is_active });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося змінити статус користувача");
      }
    }
  };

  const handleDraftChange = (userId: number, field: keyof UserDraft, value: string) => {
    setDrafts((prev) => ({
      ...prev,
      [userId]: {
        ...(prev[userId] ?? { username: "", full_name: "", email: "" }),
        [field]: value
      }
    }));
  };

  const handleSaveUserProfile = async (userId: number) => {
    const draft = drafts[userId];
    if (!draft) {
      return;
    }
    setError("");
    setPasswordResetResult("");
    try {
      await adminUpdateUser(userId, {
        username: draft.username,
        full_name: draft.full_name,
        email: draft.email
      });
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося зберегти дані користувача");
      }
    }
  };

  const openResetDialog = (userId: number) => {
    setResetUserId(userId);
    setResetPasswordValue("");
    setResetDialogOpen(true);
  };

  const handleResetPasswordConfirm = async () => {
    if (!resetUserId) {
      return;
    }
    const row = usersById.get(resetUserId);
    if (!row) {
      return;
    }

    setError("");
    setResetLoading(true);
    try {
      const result = await adminResetUserPassword(resetUserId, resetPasswordValue.trim());
      setPasswordResetResult(`Пароль для ${row.username}: ${result.new_password}`);
      setResetDialogOpen(false);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося скинути пароль");
      }
    } finally {
      setResetLoading(false);
    }
  };

  const handleRevokeSessions = async (userId: number) => {
    const row = usersById.get(userId);
    if (!row) {
      return;
    }
    setError("");
    setPasswordResetResult("");
    try {
      await adminRevokeUserSessions(userId);
      setPasswordResetResult(`Сесії користувача ${row.username} відкликано`);
      await load();
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося відкликати сесії");
      }
    }
  };

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Адмін-панель</CardTitle>
          <CardDescription>Керування інвайт-токенами, користувачами і власниками ресурсів.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {error ? <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div> : null}
          {passwordResetResult ? <div className="rounded-md border border-primary/20 bg-primary/10 p-3 text-sm">{passwordResetResult}</div> : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Токени реєстрації</CardTitle>
          <CardDescription>Поля: мітка, max_uses, expires_in_hours (0 = без строку дії).</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={handleCreateToken} className="grid gap-3 md:grid-cols-[2fr_1fr_1fr_auto]">
            <Input placeholder="Мітка (опційно)" value={label} onChange={(e) => setLabel(e.target.value)} />
            <Input
              type="number"
              min={1}
              max={1000}
              value={maxUses}
              onChange={(e) => setMaxUses(Math.max(1, Number(e.target.value) || 1))}
            />
            <Input
              type="number"
              min={0}
              max={720}
              value={expiresInHours}
              onChange={(e) => setExpiresInHours(Math.max(0, Number(e.target.value) || 0))}
            />
            <Button type="submit">Згенерувати токен</Button>
          </form>

          {issuedToken ? (
            <div className="rounded-md border border-primary/20 bg-primary/10 p-3 text-sm">
              <p>
                <span className="font-medium">Новий токен:</span> {issuedToken}
              </p>
              <p className="text-xs text-muted-foreground">Збережіть його зараз: після закриття вікна показується тільки hash.</p>
            </div>
          ) : null}

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Мітка</TableHead>
                <TableHead>Стан</TableHead>
                <TableHead>Використано</TableHead>
                <TableHead>Ким створено</TableHead>
                <TableHead>Ким використано</TableHead>
                <TableHead>Строк дії</TableHead>
                <TableHead>Дія</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tokens.map((token) => (
                <TableRow key={token.id}>
                  <TableCell>{token.id}</TableCell>
                  <TableCell>{token.label ?? "-"}</TableCell>
                  <TableCell>
                    <Badge variant={token.is_active ? (token.is_expired ? "destructive" : "secondary") : "outline"}>
                      {token.is_active ? (token.is_expired ? "Прострочений" : "Активний") : "Вимкнений"}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {token.used_count}/{token.max_uses}
                  </TableCell>
                  <TableCell>{token.created_by_username ?? token.created_by_user_id ?? "-"}</TableCell>
                  <TableCell>{token.used_by_username ?? token.used_by_user_id ?? "-"}</TableCell>
                  <TableCell>{token.expires_at ?? "-"}</TableCell>
                  <TableCell>
                    {token.is_active ? (
                      <Button variant="destructive" size="sm" type="button" onClick={() => void handleDeactivateToken(token.id)}>
                        Вимкнути
                      </Button>
                    ) : (
                      "-"
                    )}
                  </TableCell>
                </TableRow>
              ))}
              {!loading && tokens.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8}>Токенів немає.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Користувачі</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Логін</TableHead>
                <TableHead>Ім'я</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Роль</TableHead>
                <TableHead>Стан</TableHead>
                <TableHead>2FA</TableHead>
                <TableHead>Сесії</TableHead>
                <TableHead>Дії</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((row) => {
                const draft = drafts[row.id] ?? { username: row.username, full_name: row.full_name ?? "", email: row.email ?? "" };
                return (
                  <TableRow key={row.id}>
                    <TableCell>{row.id}</TableCell>
                    <TableCell>
                      <Input value={draft.username} onChange={(e) => handleDraftChange(row.id, "username", e.target.value)} />
                    </TableCell>
                    <TableCell>
                      <Input value={draft.full_name} onChange={(e) => handleDraftChange(row.id, "full_name", e.target.value)} />
                    </TableCell>
                    <TableCell>
                      <Input value={draft.email} type="email" onChange={(e) => handleDraftChange(row.id, "email", e.target.value)} />
                    </TableCell>
                    <TableCell>
                      <Select value={row.role} onValueChange={(value) => void handleUserRoleChange(row.id, value as "admin" | "user")}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="user">user</SelectItem>
                          <SelectItem value="admin">admin</SelectItem>
                        </SelectContent>
                      </Select>
                    </TableCell>
                    <TableCell>
                      <Button type="button" size="sm" variant={row.is_active ? "secondary" : "destructive"} onClick={() => void handleUserActiveToggle(row)}>
                        {row.is_active ? "Активний" : "Вимкнений"}
                      </Button>
                    </TableCell>
                    <TableCell>{row.totp_confirmed ? "Підтверджено" : "Не підтверджено"}</TableCell>
                    <TableCell>v{row.session_version}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" type="button" onClick={() => void handleSaveUserProfile(row.id)}>
                          Зберегти
                        </Button>
                        <Button size="sm" variant="secondary" type="button" onClick={() => openResetDialog(row.id)}>
                          Скинути пароль
                        </Button>
                        <Button size="sm" variant="destructive" type="button" onClick={() => void handleRevokeSessions(row.id)}>
                          Відкликати сесії
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
              {!loading && users.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9}>Користувачів немає.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <section className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Акаунти парсингу</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Мітка</TableHead>
                  <TableHead>Власник</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {resources?.accounts.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>{item.id}</TableCell>
                    <TableCell>{item.label}</TableCell>
                    <TableCell>{item.owner_username ?? item.owner_user_id ?? "-"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Цілі парсингу</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Назва</TableHead>
                  <TableHead>Власник</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {resources?.targets.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>{item.id}</TableCell>
                    <TableCell>{item.name}</TableCell>
                    <TableCell>{item.owner_username ?? item.owner_user_id ?? "-"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </section>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Привʼязки ціль ↔ акаунт</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Ціль</TableHead>
                <TableHead>Акаунт</TableHead>
                <TableHead>Власник</TableHead>
                <TableHead>Стан</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {resources?.links.map((item) => (
                <TableRow key={item.id}>
                  <TableCell>{item.id}</TableCell>
                  <TableCell>{item.target_id}</TableCell>
                  <TableCell>{item.account_id}</TableCell>
                  <TableCell>{item.owner_username ?? item.owner_user_id ?? "-"}</TableCell>
                  <TableCell>{item.is_active ? "Активна" : "Вимкнена"}</TableCell>
                </TableRow>
              ))}
              {!loading && (!resources || resources.links.length === 0) ? (
                <TableRow>
                  <TableCell colSpan={5}>Привʼязок немає.</TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={resetDialogOpen} onOpenChange={setResetDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Скидання пароля</DialogTitle>
            <DialogDescription>
              {resetUser ? `Користувач: ${resetUser.username}` : "Виберіть користувача"}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="admin-reset-password">Новий пароль (опційно)</Label>
            <Input
              id="admin-reset-password"
              type="text"
              value={resetPasswordValue}
              onChange={(e) => setResetPasswordValue(e.target.value)}
              placeholder="Порожньо = згенерувати автоматично"
            />
            <p className="text-xs text-muted-foreground">
              Якщо поле порожнє, система згенерує тимчасовий пароль.
            </p>
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setResetDialogOpen(false)} disabled={resetLoading}>
              Скасувати
            </Button>
            <Button onClick={() => void handleResetPasswordConfirm()} disabled={resetLoading || !resetUserId}>
              {resetLoading ? "Скидання..." : "Скинути пароль"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
