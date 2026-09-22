import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Shell } from "./components/layout/Shell";
import { AdminPage } from "./pages/AdminPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DataPage } from "./pages/DataPage";
import { DarknetPage } from "./pages/DarknetPage";
import { LoginPage } from "./pages/LoginPage";
import { ProfilePage } from "./pages/ProfilePage";
import { RegisterPage } from "./pages/RegisterPage";
import { TelegramPage } from "./pages/TelegramPage";
import { WhatsappPage } from "./pages/WhatsappPage";

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
