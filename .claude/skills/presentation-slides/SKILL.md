---
name: presentation-slides
description: Generate visually-appealing 16:9 PNG presentation slides (architecture/flow schematics AND data-driven figures from real backlog/eval data) for the Spec Server project, using matplotlib and following docs/presentation/STYLE_GUIDE.md, with a mandatory render → view → critique → fix review loop. Use when the user asks for slides, presentation images, diagrams, or figures to explain how the Spec Server / its concurrency model / the agent workflow works.
---

# Presentation slides

Produce polished 16:9 PNG slides for the **Spec Server** — the concurrency-safe
task/spec API for AI coding agents: the claim-next / reserve-number guarantees,
the pluggable Postgres/DynamoDB storage layer, the Cognito-auth serverless AWS
deployment, the multi-agent workflow chain, and the project-visualisation UI.

## Do this every time
1. **Read `docs/presentation/STYLE_GUIDE.md` first** — it is the authority on
   spec, palette, typography, layout, the iconography rule, data-figure/correctness
   conventions, and the review loop. If it does not exist yet, establish a concise
   one first (16:9 3200×1800, a palette, a legible sans font, an axis/units/caption
   rule, and a §review checklist), then follow it exactly.
2. **Study any existing exemplars** under `docs/presentation/` and reuse their
   helpers instead of re-inventing.
3. **Write/extend a generator script** in `docs/presentation/` (never inline
   throwaway code) and render with a Python that has matplotlib (the project venv,
   or `python3` with matplotlib installed). Output 3200×1800 PNGs named
   `NN_short_name.png`.
4. For **data-derived** slides: use REAL numbers pulled from the running Spec
   Server API (`GET /api/v1/projects/spec-server/tasks`, `/epics`, `/notes`) —
   e.g. tasks-by-status, per-epic burndown, throughput over time, chain-run
   durations. Read them; NEVER fabricate. Cite the source (endpoint + date) on the
   figure.
5. **Run the review loop (mandatory):** after each render, **Read every PNG to look
   at it**, critique against the STYLE_GUIDE checklist (tofu glyphs, overflow,
   overlaps, data/axis/convention correctness, legibility), fix, and re-render
   until clean. Report the defects you found and fixed — don't just assert it looks
   good.
6. **Deliver:** commit the script + PNGs to `docs/presentation/` on the working
   branch with a TL;DR message and the footer
   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`, then send finals with
   `SendUserFile` (`display:"render"`).

## Scaling up
For a multi-slide set, dispatch a subagent per slide (or per small group) — give
each a precise brief: content, which exemplar to base on, the exact data source if
applicable, and the output path.

## Guardrails
- Emoji/exotic glyphs render as boxes — use numbered badges or drawn shapes.
- Set explicit `vmin/vmax`; label axes with units; caption how to read each figure.
- Figures use LOCAL data (the running server / committed results) only — never
  incur AWS spend to make a slide.
