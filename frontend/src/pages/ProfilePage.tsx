import { FormEvent, useCallback, useEffect, useState } from "react";
import { ApiError, changeMyPassword, getMyProfile, revokeMySessions, updateMyProfile } from "../api";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { useAuth } from "../auth";

export function ProfilePage() {
  const { refresh } = useAuth();
  const [username, setUsername] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  const [loading, setLoading] = useState(true);
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingPassword, setSavingPassword] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const me = await getMyProfile();
      setUsername(me.username ?? "");
      setFullName(me.full_name ?? "");
      setEmail(me.email ?? "");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося завантажити профіль");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleProfileSave = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setSuccess("");
    setSavingProfile(true);
    try {
      await updateMyProfile({ username, full_name: fullName, email });
      await refresh();
      setSuccess("Профіль оновлено");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося зберегти профіль");
      }
    } finally {
      setSavingProfile(false);
    }
  };

  const handlePasswordSave = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setSuccess("");
    if (newPassword !== confirmPassword) {
      setError("Новий пароль і підтвердження не співпадають");
      return;
    }
    setSavingPassword(true);
    try {
      await changeMyPassword({ current_password: currentPassword, new_password: newPassword });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setSuccess("Пароль змінено");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Не вдалося змінити пароль");
      }
    } finally {
      setSavingPassword(false);
    }
  };

  const handleRevokeSessions = async () => {
    setError("");
    setSuccess("");
    try {
      await revokeMySessions();
      setSuccess("Інші сесії відкликано");
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
          <CardTitle className="text-xl">Особистий кабінет</CardTitle>
          <CardDescription>Керуйте своїми даними, паролем та сесіями.</CardDescription>
        </CardHeader>
        {error ? <CardContent className="pt-0"><div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</div></CardContent> : null}
        {success ? <CardContent className="pt-0"><div className="rounded-md border border-primary/20 bg-primary/10 p-3 text-sm">{success}</div></CardContent> : null}
      </Card>

      <section className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Профіль</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleProfileSave} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="profile-username">Логін</Label>
                <Input id="profile-username" value={username} onChange={(e) => setUsername(e.target.value)} required disabled={loading} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="profile-fullname">Ім'я</Label>
                <Input id="profile-fullname" value={fullName} onChange={(e) => setFullName(e.target.value)} disabled={loading} />
              </div>
              <div className="space-y-2">
                <Label htmlFor="profile-email">Email</Label>
                <Input id="profile-email" value={email} onChange={(e) => setEmail(e.target.value)} type="email" disabled={loading} />
              </div>
              <Button className="w-full" type="submit" disabled={loading || savingProfile}>
                {savingProfile ? "Збереження..." : "Зберегти"}
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Безпека</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <form onSubmit={handlePasswordSave} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="profile-current-password">Поточний пароль</Label>
                <Input id="profile-current-password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} type="password" required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="profile-new-password">Новий пароль</Label>
                <Input id="profile-new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} type="password" required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="profile-confirm-password">Підтвердження нового пароля</Label>
                <Input id="profile-confirm-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} type="password" required />
              </div>
              <Button className="w-full" type="submit" disabled={savingPassword}>
                {savingPassword ? "Оновлення..." : "Змінити пароль"}
              </Button>
            </form>
            <Button className="w-full" type="button" variant="destructive" onClick={() => void handleRevokeSessions()}>
              Відкликати інші сесії
            </Button>
          </CardContent>
        </Card>
      </section>
    </>
  );
}
