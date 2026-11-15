"""Alembic environment: always runs on the connection ``db.migrate()`` hands over."""
from alembic import context

from api.enterprise.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
