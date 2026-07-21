import { Route, Routes, useLocation } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { ActivityPage } from "./pages/ActivityPage";
import { CallbackPage } from "./pages/CallbackPage";
import { CoordinationPage } from "./pages/CoordinationPage";
import { ProgressPage } from "./pages/ProgressPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { SignInPage } from "./pages/SignInPage";
import { useAuth } from "./auth/AuthContext";
import { CALLBACK_PATHS } from "./auth/config";

/**
 * Top-level route table. UI-3..8 add further nested routes here, e.g.
 * `/projects/:slug/epics/:key`, etc. `/projects/:slug` is a stub
 * (`ProjectDetailPage`) until UI-3 fills it in.
 *
 * Auth gate (UI-7/AUTH-5): the OAuth redirect target renders outside
 * `<AppLayout>` so it never flashes the signed-out shell; everything else
 * shows the sign-in screen instead of the app when Cognito is configured
 * and there is no session (`status === "signed-out"`). When Cognito is not
 * configured, `status` is `"disabled"` and the app behaves exactly as
 * before (local-dev fallback).
 */
export default function App() {
  const location = useLocation();
  const { status } = useAuth();

  if (CALLBACK_PATHS.includes(location.pathname)) {
    return <CallbackPage />;
  }

  return (
    <AppLayout>
      {status === "signed-out" ? (
        <SignInPage />
      ) : (
        <Routes>
          <Route path="/" element={<ProjectsPage />} />
          <Route path="/projects/:slug" element={<ProjectDetailPage />} />
          <Route path="/projects/:slug/activity" element={<ActivityPage />} />
          <Route path="/projects/:slug/progress" element={<ProgressPage />} />
          <Route path="/coordination" element={<CoordinationPage />} />
        </Routes>
      )}
    </AppLayout>
  );
}
