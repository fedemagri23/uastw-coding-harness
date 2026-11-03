"""Shared fixtures for the acceptance checks. Outside the agent's writable area."""

from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import clear_mappers, sessionmaker
from sqlalchemy.pool import StaticPool

from allocation import bootstrap
from allocation.adapters.orm import metadata
from allocation.service_layer import unit_of_work


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def bus(session_factory):
    message_bus = bootstrap.bootstrap(
        start_orm=True,
        uow=unit_of_work.SqlAlchemyUnitOfWork(session_factory),
        notifications=mock.Mock(),
        publish=mock.Mock(),
    )
    message_bus.session_factory = session_factory
    yield message_bus
    clear_mappers()


@pytest.fixture
def load_batch(bus):
    """Read a batch through a brand-new unit of work, so the value really comes from the database."""

    def load(sku, ref):
        uow = unit_of_work.SqlAlchemyUnitOfWork(bus.session_factory)
        with uow:
            product = uow.products.get(sku=sku)
            batch = next(b for b in product.batches if b.reference == ref)
            return batch.expiry

    return load
