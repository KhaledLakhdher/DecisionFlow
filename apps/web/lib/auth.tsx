"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, clearTokens, hasSession, type Me, setTokens } from "./api";

type AuthState = {
  me: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (payload: {
    email: string;
    password: string;
    full_name: string;
    organization_name: string;
  }) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  // On mount, a refresh token in storage means there may be a live session.
  // The access token lives in memory only, so it is always absent here — the
  // /me call fails, triggers a refresh, and replays. That round trip is what
  // makes a page reload keep the user signed in.
  useEffect(() => {
    if (!hasSession()) {
      setLoading(false);
      return;
    }
    api
      .me()
      .then(setMe)
      .catch(() => clearTokens())
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const tokens = await api.login(email, password);
    setTokens(tokens.access_token, tokens.refresh_token);
    setMe(await api.me());
  }, []);

  const register = useCallback(
    async (payload: Parameters<typeof api.register>[0]) => {
      const tokens = await api.register(payload);
      setTokens(tokens.access_token, tokens.refresh_token);
      setMe(await api.me());
    },
    [],
  );

  const logout = useCallback(() => {
    clearTokens();
    setMe(null);
    router.push("/login");
  }, [router]);

  return (
    <AuthContext.Provider value={{ me, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

/** Redirect to /login when there is no session. */
export function useRequireAuth() {
  const { me, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !me) router.replace("/login");
  }, [loading, me, router]);

  return { me, loading };
}
