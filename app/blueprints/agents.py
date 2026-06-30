"""Agent registry (lightweight; ownership is by slug string)."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint

from ..extensions import db
from ..helpers import require_api_key
from ..models import Agent
from ..schemas import AgentIn, AgentOut

blp = Blueprint(
    "agents", __name__, url_prefix="/api/v1/agents",
    description="Agent/actor registry.",
)


@blp.route("")
class AgentsCollection(MethodView):
    @blp.response(200, AgentOut(many=True))
    def get(self):
        """List registered agents."""
        require_api_key()
        return db.session.execute(
            sa.select(Agent).order_by(Agent.slug)
        ).scalars().all()

    @blp.arguments(AgentIn)
    @blp.response(201, AgentOut)
    def post(self, data):
        """Register an agent (idempotent upsert by slug)."""
        require_api_key()
        agent = db.session.execute(
            sa.select(Agent).where(Agent.slug == data["slug"])
        ).scalar_one_or_none()
        if agent is None:
            agent = Agent(**data)
            db.session.add(agent)
        else:
            for k, v in data.items():
                setattr(agent, k, v)
        db.session.commit()
        return agent
