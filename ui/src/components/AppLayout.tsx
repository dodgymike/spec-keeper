import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import "./AppLayout.css";

interface AppLayoutProps {
  children: ReactNode;
}

/** The app shell: a header with nav, and a content area for routed pages. */
export function AppLayout({ children }: AppLayoutProps) {
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
        </nav>
      </header>
      <main className="app-layout__content">{children}</main>
    </div>
  );
}
