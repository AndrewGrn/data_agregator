import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { ApiError, getMe, login as apiLogin, logout as apiLogout, type User } from "./api";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string, otpCode: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try {
      const currentUser = await getMe();
      setUser(currentUser);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setUser(null);
        return;
      }
      throw error;
    }
  };

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, []);

  const login = async (username: string, password: string, otpCode: string) => {
    const currentUser = await apiLogin(username, password, otpCode);
    setUser(currentUser);
  };

  const logout = async () => {
    await apiLogout();
    setUser(null);
  };

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      login,
      logout,
      refresh
    }),
    [user, loading]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
