import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import * as session from "./session";
import type { AuthState } from "./session";

interface AuthContextValue extends AuthState {
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/** Wraps `auth/session.ts`'s singleton for React consumers (header, settings page). */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(session.getState());

  useEffect(() => session.subscribe(setState), []);

  const value: AuthContextValue = {
    ...state,
    signOut: session.signOut,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth() must be used within an <AuthProvider>.");
  }
  return ctx;
}
