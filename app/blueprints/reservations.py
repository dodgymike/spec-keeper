"""Atomic, collision-proof number reservation (migration/table/queue numbers)."""
from __future__ import annotations

from flask import current_app, jsonify, request
from flask.views import MethodView
from flask_smorest import Blueprint

from ..helpers import require_api_key
from ..idempotency import idempotency_key_from_request
from ..schemas import CounterOut, ReservationIn, ReservationOut

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
        return current_app.storage.list_reservations(slug, request.args.get("namespace"))

    @blp.arguments(ReservationIn)
    @blp.response(201, ReservationOut)
    def post(self, data, slug):
        """Atomically reserve the next number in a namespace.

        Concurrent callers on the same namespace get distinct, increasing
        values — no two agents can ever be handed the same number."""
        require_api_key()
        result = current_app.storage.reserve_number(
            slug,
            data["namespace"],
            reserved_by=data.get("reserved_by"),
            task_key=data.get("task_key"),
            note=data.get("note"),
            idempotency_key=idempotency_key_from_request(),
            serialize=lambda res: ReservationOut().dump(res),
        )
        if result.replay_body is not None:
            return jsonify(result.replay_body), result.replay_status
        return result.result


@blp.route("/counters")
class CountersCollection(MethodView):
    @blp.response(200, CounterOut(many=True))
    def get(self, slug):
        """Current counter values per namespace."""
        require_api_key()
        return current_app.storage.list_counters(slug)
