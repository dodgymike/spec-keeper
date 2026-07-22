"""ISO-7: fail-closed boot guard for the project-isolation flag.

``PROJECT_ISOLATION_ENFORCED`` (ISO-4) authorizes project-scoped routes off the
VERIFIED caller identity, which only exists on the Cognito JWT path. Turning it
ON while ``COGNITO_ISSUER`` is unset (local/auth-off mode) would brick every
project-scoped route (403/404). ``create_app`` must refuse to boot in that combo
rather than come up misleadingly broken; every other combination boots fine.
"""
from __future__ import annotations

import pytest

from app import create_app
from app.config import TestConfig


def test_isolation_on_without_cognito_refuses_to_boot():
    class Cfg(TestConfig):
        PROJECT_ISOLATION_ENFORCED = True
        COGNITO_ISSUER = None

    with pytest.raises(RuntimeError) as exc:
        create_app(Cfg)

    msg = str(exc.value)
    assert "PROJECT_ISOLATION_ENFORCED" in msg
    assert "COGNITO_ISSUER" in msg


def test_isolation_off_boots():
    class Cfg(TestConfig):
        PROJECT_ISOLATION_ENFORCED = False
        COGNITO_ISSUER = None

    # Must not raise.
    assert create_app(Cfg) is not None


def test_isolation_on_with_cognito_boots():
    class Cfg(TestConfig):
        PROJECT_ISOLATION_ENFORCED = True
        COGNITO_ISSUER = "https://cognito-idp.test.amazonaws.com/us-east-1_BOOTPOOL"

    # Enforcement + configured auth is the valid deployed combination.
    assert create_app(Cfg) is not None
