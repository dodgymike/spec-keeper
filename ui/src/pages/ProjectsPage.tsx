import { useEffect, useState } from "react";
import { ApiError, listProjects } from "../api/client";
import type { Project } from "../api/types";
import { Card } from "../components/Card";
import "./ProjectsPage.css";

type LoadState =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; projects: Project[] };

export function ProjectsPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    listProjects()
      .then((projects) => {
        if (!cancelled) setState({ status: "ready", projects });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            error: error instanceof Error ? error : new Error(String(error)),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="projects-page">
      <header className="projects-page__header">
        <h1 className="projects-page__title">Projects</h1>
      </header>

      {state.status === "loading" && <ProjectsSkeleton />}

      {state.status === "error" && (
        <Card className="projects-page__error">
          <p>Could not load projects from the Spec Server API.</p>
          <p className="projects-page__error-detail">{state.error.message}</p>
        </Card>
      )}

      {state.status === "ready" && state.projects.length === 0 && (
        <Card>
          <p>No projects yet.</p>
        </Card>
      )}

      {state.status === "ready" && state.projects.length > 0 && (
        <div className="projects-page__grid">
          {state.projects.map((project) => (
            <Card key={project.public_id} className="project-card">
              <h2 className="project-card__name">{project.name}</h2>
              <p className="project-card__slug">{project.slug}</p>
              {project.description && (
                <p className="project-card__description">{project.description}</p>
              )}
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

function ProjectsSkeleton() {
  return (
    <div className="projects-page__grid" aria-busy="true" aria-label="Loading projects">
      {[0, 1, 2].map((i) => (
        <Card key={i} className="project-card project-card--skeleton">
          <div className="skeleton-line skeleton-line--title" />
          <div className="skeleton-line skeleton-line--slug" />
          <div className="skeleton-line skeleton-line--body" />
        </Card>
      ))}
    </div>
  );
}
