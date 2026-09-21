import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { Button } from "./components/ui/button";
import { cn } from "./lib/utils";
import { AdminPage } from "./pages/AdminPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DataPage } from "./pages/DataPage";
import { DarknetPage } from "./pages/DarknetPage";
import { LoginPage } from "./pages/LoginPage";
import { ProfilePage } from "./pages/ProfilePage";
import { RegisterPage } from "./pages/RegisterPage";
import { TelegramPage } from "./pages/TelegramPage";
import { WhatsappPage } from "./pages/WhatsappPage";

type NavItem = {
  to: string;
  label: string;
  adminOnly?: boolean;
};

const navItems: NavItem[] = [
  { to: "/", label: "Панель" },
  { to: "/telegram", label: "Telegram" },
  { to: "/whatsapp", label: "WhatsApp" },
  { to: "/darknet", label: "Darknet" },
  { to: "/data", label: "Дані" },
  { to: "/profile", label: "Кабінет" },
  { to: "/admin", label: "Адмін", adminOnly: true }
];

function Shell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl items-center justify-between gap-3 px-4 py-3">
          <div className="text-sm font-semibold tracking-wide">Агрегатор Даних</div>
          <nav className="flex flex-wrap items-center gap-1">
            {navItems
              .filter((item) => !item.adminOnly || user?.role === "admin")
              .map((item) => (
                <Link
                  key={item.to}
                  to={item.to}
                  className={cn(
                    "rounded-md px-3 py-2 text-sm font-medium transition-colors",
                    location.pathname === item.to ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"
                  )}
                >
                  {item.label}
                </Link>
              ))}
          </nav>
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted-foreground">
              {user?.username} ({user?.role === "admin" ? "admin" : "user"})
            </span>
            <Button type="button" variant="destructive" size="sm" onClick={handleLogout}>
              Вийти
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-6">{children}</main>
    </div>
  );
}

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">Завантаження...</div>;
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

function AdminOnly({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) {
    return <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">Завантаження...</div>;
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  if (user.role !== "admin") {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route
        path="/"
        element={
          <Protected>
            <Shell>
              <DashboardPage />
            </Shell>
          </Protected>
        }
      />
      <Route
        path="/telegram"
        element={
          <Protected>
            <Shell>
              <TelegramPage />
            </Shell>
          </Protected>
        }
      />
      <Route
        path="/whatsapp"
        element={
          <Protected>
            <Shell>
              <WhatsappPage />
            </Shell>
          </Protected>
        }
      />
      <Route
        path="/darknet"
        element={
          <Protected>
            <Shell>
              <DarknetPage />
            </Shell>
          </Protected>
        }
      />
      <Route
        path="/data"
        element={
          <Protected>
            <Shell>
              <DataPage />
            </Shell>
          </Protected>
        }
      />
      <Route
        path="/profile"
        element={
          <Protected>
            <Shell>
              <ProfilePage />
            </Shell>
          </Protected>
        }
      />
      <Route
        path="/admin"
        element={
          <Protected>
            <AdminOnly>
              <Shell>
                <AdminPage />
              </Shell>
            </AdminOnly>
          </Protected>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
