"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { RequestError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { me, login, register } = useAuth();
  const router = useRouter();

  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (me) router.replace("/");
  }, [me, router]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (mode === "login") {
        await login(email, password);
      } else {
        await register({
          email,
          password,
          full_name: fullName,
          organization_name: orgName,
        });
      }
      router.replace("/");
    } catch (err) {
      // Surface the API's own message — it distinguishes "incorrect password"
      // from "rate limited", which matters to someone stuck at a login screen.
      setError(err instanceof RequestError ? err.error.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <h1 className="text-2xl font-semibold mb-1">DecisionFlow</h1>
        <p className="text-sm mb-6" style={{ color: "var(--text-secondary)" }}>
          Turn your data into decisions.
        </p>

        <form onSubmit={submit} className="card p-5 flex flex-col gap-3">
          {mode === "register" && (
            <>
              <label className="text-sm">
                Your name
                <input
                  className="input mt-1"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  required
                />
              </label>
              <label className="text-sm">
                Workspace name
                <input
                  className="input mt-1"
                  value={orgName}
                  onChange={(e) => setOrgName(e.target.value)}
                  required
                />
              </label>
            </>
          )}

          <label className="text-sm">
            Email
            <input
              type="email"
              className="input mt-1"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </label>

          <label className="text-sm">
            Password
            <input
              type="password"
              className="input mt-1"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={mode === "register" ? 12 : undefined}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
            />
            {mode === "register" && (
              <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                At least 12 characters.
              </span>
            )}
          </label>

          {error && (
            <p
              role="alert"
              className="text-sm px-3 py-2 rounded"
              style={{ background: "var(--page)", color: "var(--status-critical)" }}
            >
              {error}
            </p>
          )}

          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Working…" : mode === "login" ? "Sign in" : "Create workspace"}
          </button>

          <button
            type="button"
            className="text-sm underline underline-offset-2"
            style={{ color: "var(--text-secondary)" }}
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError(null);
            }}
          >
            {mode === "login" ? "Create a new workspace" : "I already have an account"}
          </button>
        </form>
      </div>
    </main>
  );
}
