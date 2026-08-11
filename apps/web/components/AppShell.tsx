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
          <Link href="/" className="font-semibold">
            DecisionFlow
          </Link>
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
