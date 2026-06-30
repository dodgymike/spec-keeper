"""Shared Flask extension singletons.

Kept in their own module so models/blueprints can import them without
triggering circular imports through the app factory.
"""
from __future__ import annotations

from flask_smorest import Api
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


db = SQLAlchemy(model_class=Base)
api = Api()
