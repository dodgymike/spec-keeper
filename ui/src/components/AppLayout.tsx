import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { isAdminUser } from "../auth/session";
import { AutoRefreshControl } from "./AutoRefreshControl";
import "./AppLayout.css";

interface AppLayoutProps {
  children: ReactNode;
}

/** The app shell: a header with nav + auth status, and a content area for routed pages. */
export function AppLayout({ children }: AppLayoutProps) {
  const { status, user, signOut } = useAuth();
  const showAdmin = status === "signed-in" && isAdminUser(user);

  return (
    <div className="app-layout">
      <header className="app-layout__header">
        <span className="app-layout__brand">Spec Server</span>
        <nav className="app-layout__nav">
          <NavLink
            to="/"
            end
            className={({ isActive }) =>
              isActive ? "app-layout__link app-layout__link--active" : "app-layout__link"
            }
          >
            Projects
          </NavLink>
          <NavLink
            to="/coordination"
            className={({ isActive }) =>
              isActive ? "app-layout__link app-layout__link--active" : "app-layout__link"
            }
          >
            Coordination
          </NavLink>
          {showAdmin ? (
            <NavLink
              to="/admin"
              className={({ isActive }) =>
                isActive ? "app-layout__link app-layout__link--active" : "app-layout__link"
              }
            >
              Admin
            </NavLink>
          ) : null}
          {status === "signed-in" ? (
            <NavLink
              to="/settings"
              className={({ isActive }) =>
                isActive ? "app-layout__link app-layout__link--active" : "app-layout__link"
              }
            >
              Settings
            </NavLink>
          ) : null}
        </nav>
        {status === "signed-in" ? <AutoRefreshControl /> : null}
        <div className="app-layout__auth" role="status" aria-live="polite">
          {status === "signed-in" ? (
            <>
              <span className="app-layout__user">
                {user?.email ? `Signed in as ${user.email}` : "Signed in"}
              </span>
              <button type="button" className="app-layout__signout" onClick={() => void signOut()}>
                Sign out
              </button>
            </>
          ) : null}
        </div>
      </header>
      <main className="app-layout__content">{children}</main>
    </div>
  );
}
