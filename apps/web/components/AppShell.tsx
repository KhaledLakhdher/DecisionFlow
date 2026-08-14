"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { me, logout } = useAuth();
  const workspace = me?.memberships.find((m) => m.org_id === me.active_org_id);

  return (
    <div className="min-h-screen">
      <header
        className="border-b sticky top-0 z-10"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      >
        <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-6">
            <Link href="/" className="font-semibold">
              DecisionFlow
            </Link>
            <nav className="flex items-center gap-4 text-sm">
              <Link href="/" style={{ color: "var(--text-secondary)" }}>
                Datasets
              </Link>
              <Link href="/model" style={{ color: "var(--text-secondary)" }}>
                Data model
              </Link>
            </nav>
          </div>
          <div className="flex items-center gap-4 text-sm">
            {workspace && (
              <span style={{ color: "var(--text-secondary)" }}>
                {workspace.organization.name}
                <span style={{ color: "var(--text-muted)" }}> · {workspace.role}</span>
              </span>
            )}
            <button
              onClick={logout}
              className="underline underline-offset-2"
              style={{ color: "var(--text-secondary)" }}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-6 py-8">{children}</main>
    </div>
  );
}
