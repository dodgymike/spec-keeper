"""Per-project Jira integration config CRUD (JIRA-5)."""
from __future__ import annotations

import sqlalchemy as sa
from flask.views import MethodView
from flask_smorest import Blueprint, abort

from ..crypto import encrypt
from ..extensions import db
from ..helpers import get_project_or_404, require_api_key
from ..models import JiraProjectConfig
from ..schemas import JiraConfigIn, JiraConfigOut, JiraConfigUpdate

blp = Blueprint(
    "jira_config", __name__, url_prefix="/api/v1/projects",
    description="Per-project Jira integration configuration.",
)


def _get_config_or_404(project_id: int) -> JiraProjectConfig:
    config = db.session.execute(
        sa.select(JiraProjectConfig).where(
            JiraProjectConfig.project_id == project_id
        )
    ).scalar_one_or_none()
    if config is None:
        abort(404, message="Jira config not found for this project.")
    return config


def _config_to_out(config: JiraProjectConfig) -> dict:
    """Build the output dict — never includes the encrypted token."""
    return {
        "base_url": config.base_url,
        "email": config.email,
        "jira_project_key": config.jira_project_key,
        "enabled": config.enabled,
        "has_token": config.api_token_encrypted is not None,
        "updated_at": config.updated_at,
    }


@blp.route("/<slug>/jira-config")
class JiraConfigResource(MethodView):
    @blp.response(200, JiraConfigOut)
    def get(self, slug):
        """Get the Jira integration config for a project."""
        require_api_key()
        project = get_project_or_404(slug)
        config = _get_config_or_404(project.id)
        return _config_to_out(config)

    @blp.arguments(JiraConfigIn)
    @blp.response(201, JiraConfigOut)
    def post(self, data, slug):
        """Create Jira integration config for a project."""
        require_api_key()
        project = get_project_or_404(slug)

        # Check for existing config
        existing = db.session.execute(
            sa.select(JiraProjectConfig).where(
                JiraProjectConfig.project_id == project.id
            )
        ).scalar_one_or_none()
        if existing is not None:
            abort(409, message="Jira config already exists for this project. Use PUT to update.")

        encrypted_token = encrypt(data["api_token"])
        config = JiraProjectConfig(
            project_id=project.id,
            base_url=data["base_url"],
            email=data["email"],
            api_token_encrypted=encrypted_token,
            jira_project_key=data["jira_project_key"],
            enabled=data.get("enabled", False),
        )
        db.session.add(config)
        db.session.commit()
        return _config_to_out(config)

    @blp.arguments(JiraConfigUpdate)
    @blp.response(200, JiraConfigOut)
    def put(self, data, slug):
        """Update Jira integration config for a project."""
        require_api_key()
        project = get_project_or_404(slug)
        config = _get_config_or_404(project.id)

        if "base_url" in data:
            config.base_url = data["base_url"]
        if "email" in data:
            config.email = data["email"]
        if "api_token" in data:
            config.api_token_encrypted = encrypt(data["api_token"])
        if "jira_project_key" in data:
            config.jira_project_key = data["jira_project_key"]
        if "enabled" in data:
            config.enabled = data["enabled"]

        db.session.commit()
        return _config_to_out(config)
