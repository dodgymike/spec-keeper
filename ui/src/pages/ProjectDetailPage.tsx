import { Link, useParams } from "react-router-dom";
import { Card } from "../components/Card";
import "./ProjectDetailPage.css";

/**
 * Stub project detail screen. UI-3 replaces this with the real epic/task
 * board for the project; for now it just confirms routing works and gives
 * a way back to the overview.
 */
export function ProjectDetailPage() {
  const { slug } = useParams<{ slug: string }>();

  return (
    <section className="project-detail-page">
      <p className="project-detail-page__back">
        <Link to="/">&larr; Projects</Link>
      </p>
      <Card>
        <h1 className="project-detail-page__slug">{slug}</h1>
        <p className="project-detail-page__placeholder">Coming soon.</p>
      </Card>
    </section>
  );
}
