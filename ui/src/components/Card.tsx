import type { ReactNode } from "react";
import "./Card.css";

interface CardProps {
  children: ReactNode;
  className?: string;
}

/** A raised surface used for list items, summaries, and panels. */
export function Card({ children, className }: CardProps) {
  const classes = className ? `card ${className}` : "card";
  return <div className={classes}>{children}</div>;
}
