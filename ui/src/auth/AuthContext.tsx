import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import * as session from "./session";
import type { AuthState } from "./session";

interface AuthContextValue extends AuthState {
  signIn: (returnTo?: string) => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/** Wraps `auth/session.ts`'s singleton for React consumers (header, sign-in screen). */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(session.getState());

  useEffect(() => session.subscribe(setState), []);

  const value: AuthContextValue = {
    ...state,
    signIn: session.signIn,
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
