import { Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { ProjectsPage } from "./pages/ProjectsPage";

/**
 * Top-level route table. UI-2..8 add nested routes here, e.g.
 * `/projects/:slug`, `/projects/:slug/epics/:key`, etc.
 */
export default function App() {
  return (
    <AppLayout>
      <Routes>
        <Route path="/" element={<ProjectsPage />} />
      </Routes>
    </AppLayout>
  );
}
