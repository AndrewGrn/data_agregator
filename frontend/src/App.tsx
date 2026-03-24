import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import { Setup2FAPage } from "./pages/Setup2FAPage";
import { DashboardPage } from "./pages/DashboardPage";
import { TelegramPage } from "./pages/TelegramPage";
import { DataPage } from "./pages/DataPage";
import { AdminPage } from "./pages/AdminPage";

function Shell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  return (
    <div className="app-root">
      <header className="topbar">
        <div className="brand">Агрегатор Даних</div>
        <nav>
          <Link to="/">Панель</Link>
          <Link to="/telegram">Telegram</Link>
          <Link to="/data">Дані</Link>
          {user?.role === "admin" ? <Link to="/admin">Адмін</Link> : null}
          {user?.role === "admin" ? (
            <a href="http://localhost:8000/modules/darknet" target="_blank" rel="noreferrer">
              Darknet
            </a>
          ) : null}
        </nav>
        <div className="topbar-right">
          <span className="user-label">
            {user?.username} ({user?.role === "admin" ? "admin" : "user"})
          </span>
          <button className="danger" onClick={handleLogout} type="button">
            Вийти
          </button>
        </div>
      </header>
      <main className="container">{children}</main>
    </div>
  );
}

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return <div className="centered">Завантаження...</div>;
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

function AdminOnly({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) {
    return <div className="centered">Завантаження...</div>;
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
      <Route path="/setup-2fa" element={<Setup2FAPage />} />
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
