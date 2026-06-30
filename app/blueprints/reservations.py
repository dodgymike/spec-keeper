"""Atomic, collision-proof number reservation (migration/table/queue numbers)."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint

from ..extensions import db
from ..helpers import get_project_or_404, get_task_or_404, require_api_key
from ..models import Counter, Reservation
from ..schemas import CounterOut, ReservationIn, ReservationOut
from ..services import reserve_number

blp = Blueprint(
    "reservations", __name__, url_prefix="/api/v1/projects/<slug>",
    description="Reserve collision-proof numbers in a namespace.",
)


@blp.route("/reservations")
class ReservationsCollection(MethodView):
    @blp.response(200, ReservationOut(many=True))
    def get(self, slug):
        """List reservations (audit trail), optionally by ``?namespace=``."""
        require_api_key()
        from flask import request

        project = get_project_or_404(slug)
        query = sa.select(Reservation).where(Reservation.project_id == project.id)
        ns = request.args.get("namespace")
        if ns:
            query = query.where(Reservation.namespace == ns)
        return db.session.execute(
            query.order_by(Reservation.namespace, Reservation.value)
        ).scalars().all()

    @blp.arguments(ReservationIn)
    @blp.response(201, ReservationOut)
    def post(self, data, slug):
        """Atomically reserve the next number in a namespace.

        Concurrent callers on the same namespace get distinct, increasing
        values — no two agents can ever be handed the same number."""
        require_api_key()
        project = get_project_or_404(slug)
        task_id = None
        if data.get("task_key"):
            task_id = get_task_or_404(project.id, data["task_key"]).id
        reservation = reserve_number(
            project_id=project.id,
            namespace=data["namespace"],
            reserved_by=data.get("reserved_by"),
            task_id=task_id,
            note=data.get("note"),
        )
        db.session.commit()
        return reservation


@blp.route("/counters")
class CountersCollection(MethodView):
    @blp.response(200, CounterOut(many=True))
    def get(self, slug):
        """Current counter values per namespace."""
        require_api_key()
        project = get_project_or_404(slug)
        return db.session.execute(
            sa.select(Counter)
            .where(Counter.project_id == project.id)
            .order_by(Counter.namespace)
        ).scalars().all()
