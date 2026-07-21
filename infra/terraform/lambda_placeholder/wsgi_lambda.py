# wsgi_lambda.py — PLACEHOLDER Lambda handler (INFRA-3).
# =============================================================================
# This is a stand-in so `terraform validate`/`plan` and a first `apply` have a
# real (if inert) code artifact WITHOUT depending on a build pipeline. It is NOT
# the production handler.
#
# INFRA-4 (the build pipeline) + the app's WSGI-handler task MUST replace this
# with the real adapter: a WSGI bridge (e.g. Mangum or aws-wsgi/apig-wsgi) that
# wraps the Flask `create_app()` application and translates API Gateway HTTP API
# (payload format 2.0) events <-> WSGI. The exported symbol name MUST stay
# `handler` so the Lambda `handler = "wsgi_lambda.handler"` wiring keeps working.
#
# Reference shape of the real file (do not enable here — Mangum/flask are not
# vendored in this placeholder zip):
#
#     from mangum import Mangum
#     from app import create_app
#     handler = Mangum(create_app(), lifespan="off")
#
# Until then this stub answers every request with 503 so a misconfigured deploy
# is obvious rather than silently serving nothing.
# =============================================================================

import json


def handler(event, context):  # noqa: D401  (placeholder)
    """Inert placeholder. Returns 503 until INFRA-4 ships the real WSGI bridge."""
    return {
        "statusCode": 503,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "error": "not_implemented",
                "detail": (
                    "Placeholder Lambda artifact. INFRA-4 must build and deploy "
                    "the real WSGI (Mangum/aws-wsgi) handler wrapping the Flask app."
                ),
            }
        ),
    }
