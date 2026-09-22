import {
  Database,
  LayoutDashboard,
  LogOut,
  MessageCircle,
  Send,
  ShieldAlert,
  ShieldCheck,
  UserCircle
} from "lucide-react";
import type * as React from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../../auth";
import { cn } from "../../lib/utils";
import { IconButton } from "../ui/icon-button";

type NavItem = {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  adminOnly?: boolean;
};

const navItems: NavItem[] = [
  { to: "/", label: "Дашборд", icon: LayoutDashboard },
  { to: "/telegram", label: "Telegram", icon: Send },
  { to: "/whatsapp", label: "WhatsApp", icon: MessageCircle },
  { to: "/darknet", label: "Darknet", icon: ShieldAlert },
  { to: "/data", label: "Дані", icon: Database },
  { to: "/profile", label: "Кабінет", icon: UserCircle },
  { to: "/admin", label: "Адмін", icon: ShieldCheck, adminOnly: true }
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const activeItem = navItems.find((item) => item.to === location.pathname);

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="sticky top-0 flex h-screen w-16 shrink-0 flex-col items-center border-r border-border bg-card py-4 sm:w-56 sm:items-stretch sm:px-3">
        <div className="mb-4 flex items-center gap-2 px-1 sm:px-2">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary text-sm font-semibold text-primary-foreground">
            А
          </div>
          <span className="hidden truncate text-sm font-semibold text-foreground sm:inline">Агрегатор Даних</span>
        </div>

        <nav className="flex flex-1 flex-col gap-1">
          {navItems
            .filter((item) => !item.adminOnly || user?.role === "admin")
            .map((item) => {
              const isActive = item.to === "/" ? location.pathname === "/" : location.pathname.startsWith(item.to);
              const Icon = item.icon;
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  title={item.label}
                  className={cn(
                    "flex h-9 items-center justify-center gap-2 rounded-md px-0 text-sm font-medium transition-colors sm:justify-start sm:px-2.5",
                    isActive ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
                  )}
                >
                  <Icon className="size-4 shrink-0" />
                  <span className="hidden truncate sm:inline">{item.label}</span>
                </Link>
              );
            })}
        </nav>

        <div className="mt-4 flex flex-col items-center gap-2 border-t border-border pt-3 sm:items-stretch">
          <span className="hidden truncate px-1 text-xs text-muted-foreground sm:block">
            {user?.username} ({user?.role === "admin" ? "admin" : "user"})
          </span>
          <div className="flex justify-center sm:justify-start">
            <IconButton label="Вийти" icon={LogOut} tone="danger" onClick={handleLogout} />
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 border-b border-border bg-background/95 px-4 py-3 backdrop-blur sm:px-6">
          <h1 className="text-sm font-semibold text-foreground">{activeItem?.label ?? "Агрегатор Даних"}</h1>
        </header>
        <main className="flex flex-1 flex-col gap-4 px-4 py-6 sm:px-6">{children}</main>
      </div>
    </div>
  );
}
