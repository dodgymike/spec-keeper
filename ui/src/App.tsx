import { Route, Routes } from "react-router-dom";
import { AppLayout } from "./components/AppLayout";
import { CoordinationPage } from "./pages/CoordinationPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectsPage } from "./pages/ProjectsPage";

/**
 * Top-level route table. UI-3..8 add further nested routes here, e.g.
 * `/projects/:slug/epics/:key`, etc. `/projects/:slug` is a stub
 * (`ProjectDetailPage`) until UI-3 fills it in.
 */
export default function App() {
  return (
    <AppLayout>
      <Routes>
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/projects/:slug" element={<ProjectDetailPage />} />
        <Route path="/coordination" element={<CoordinationPage />} />
      </Routes>
    </AppLayout>
  );
}
