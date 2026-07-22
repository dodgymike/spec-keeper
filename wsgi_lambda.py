"""AWS Lambda entry point for the Spec Server (INFRA-4).

Replaces the INFRA-3 placeholder (``infra/terraform/lambda_placeholder``) with
the REAL handler: the existing Flask app (``app.create_app()``) served on AWS
Lambda behind an API Gateway **HTTP API** (payload format **2.0**).

Adapter choice — Mangum + a2wsgi
--------------------------------
Flask is a **WSGI** app; Mangum is an **ASGI** Lambda adapter. So we bridge in
two hops:

    Flask (WSGI)  --a2wsgi.WSGIMiddleware-->  ASGI  --Mangum-->  Lambda/API GW

* ``mangum``  — maintained ASGI<->Lambda adapter; understands API Gateway HTTP
  API v2.0 events and does the event<->request translation.
* ``a2wsgi``  — tiny, zero-dependency, maintained WSGI<->ASGI shim. It runs the
  synchronous Flask app in a threadpool so Mangum can drive it.

Both are pure-Python wheels (no compiled extensions), so the arm64 build in
``scripts/build_lambda.sh`` is trivial. A single-dependency alternative is
``apig-wsgi`` (pure WSGI, native HTTP API v2.0); Mangum is used here because it
is the choice the INFRA-3 wiring and terraform comments already reference and
the ``handler = "wsgi_lambda.handler"`` name stays stable either way.

Cold-start discipline
---------------------
Everything expensive is built ONCE at **module import** (cold start) and reused
across warm invocations: ``create_app()`` (which constructs the storage backend
and its boto3 DynamoDB resource/client — boto3 clients are lazy and make no
network call until first use), the WSGI->ASGI wrap, and the Mangum handler. The
Lambda runtime imports this module once per execution environment and then calls
``handler(event, context)`` per request, so none of this is rebuilt per request.

Read-only filesystem / env-driven config
----------------------------------------
Lambda's filesystem is read-only except ``/tmp``. This handler never writes to
CWD. All configuration (``STORAGE_BACKEND=dynamodb``, ``DDB_TABLE`` /
``DYNAMODB_TABLE``, ``COGNITO_*``, region) comes from environment variables set
by ``infra/terraform/lambda.tf``. Schema creation / migrations (``flask init-db``,
``alembic upgrade``) are **NOT** run here — that is a one-time deploy step owned
by the deploy-coordinator, never something the request path should do.
"""
from __future__ import annotations

from a2wsgi import WSGIMiddleware
from mangum import Mangum

from app import create_app

# --- Cold-start init (module scope, reused across warm invocations) -------- #
# create_app() wires config from os.environ and builds the storage backend
# (DynamoDB on Lambda). No network I/O happens here.
_wsgi_app = create_app()

# Wrap the WSGI (Flask) app as ASGI so Mangum can serve it.
_asgi_app = WSGIMiddleware(_wsgi_app)

# The Lambda handler. lifespan="off": WSGI apps have no ASGI lifespan protocol,
# so disabling it avoids a startup handshake Mangum would otherwise attempt.
# Mangum's default api_gateway_base_path ("/") already keeps the HTTP API
# default-stage paths intact, so no extra kwarg is needed.
handler = Mangum(_asgi_app, lifespan="off")
