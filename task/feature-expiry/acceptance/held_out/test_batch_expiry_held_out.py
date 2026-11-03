"""Held-out acceptance cases. They run only in final verification; the agent never sees them."""

from datetime import date, timedelta

import pytest

from allocation import views
from allocation.domain import commands
from allocation.service_layer import handlers, unit_of_work

today = date.today()
yesterday = today - timedelta(days=1)
tomorrow = today + timedelta(days=1)
later = today + timedelta(days=30)


def test_end_to_end_allocation_skips_a_batch_loaded_as_expired(bus):
    bus.handle(commands.CreateBatch("expired", "LAMP", 10, None, yesterday))
    bus.handle(commands.CreateBatch("good", "LAMP", 10, tomorrow, later))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert views.allocations("o1", bus.uow) == [{"sku": "LAMP", "batchref": "good"}]


def test_end_to_end_only_expired_stock_allocates_nothing(bus):
    bus.handle(commands.CreateBatch("expired", "LAMP", 10, None, yesterday))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert views.allocations("o1", bus.uow) == []


def test_rejected_batch_is_not_saved(bus):
    with pytest.raises(handlers.InvalidExpiry):
        bus.handle(commands.CreateBatch("bad", "LAMP", 10, later, tomorrow))
    uow = unit_of_work.SqlAlchemyUnitOfWork(bus.session_factory)
    with uow:
        product = uow.products.get(sku="LAMP")
        assert product is None or all(batch.reference != "bad" for batch in product.batches)


def test_batch_without_expiry_still_allocates_end_to_end(bus):
    bus.handle(commands.CreateBatch("plain", "LAMP", 10))
    bus.handle(commands.Allocate("o1", "LAMP", 3))
    assert views.allocations("o1", bus.uow) == [{"sku": "LAMP", "batchref": "plain"}]


def test_changing_quantity_still_works_on_a_batch_with_expiry(bus):
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, None, later))
    bus.handle(commands.Allocate("o1", "LAMP", 8))
    bus.handle(commands.ChangeBatchQuantity("b1", 9))
    assert views.allocations("o1", bus.uow) == [{"sku": "LAMP", "batchref": "b1"}]


def test_invalid_sku_error_is_unchanged(bus):
    with pytest.raises(handlers.InvalidSku):
        bus.handle(commands.Allocate("o1", "NOPE", 1))
