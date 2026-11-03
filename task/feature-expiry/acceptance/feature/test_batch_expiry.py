"""Feature check for optional batch expiry. The harness returns failures to the agent."""

from datetime import date, timedelta

import pytest

from allocation.domain import commands, events
from allocation.domain.model import Batch, OrderLine, Product
from allocation.service_layer import handlers

today = date.today()
yesterday = today - timedelta(days=1)
tomorrow = today + timedelta(days=1)
later = today + timedelta(days=30)


# --- existing callers ---------------------------------------------------------


def test_batch_and_command_still_work_without_expiry():
    assert Batch("b1", "LAMP", 10, eta=None).expiry is None
    assert commands.CreateBatch("b1", "LAMP", 10).expiry is None
    assert commands.CreateBatch("b1", "LAMP", 10, tomorrow).eta == tomorrow


def test_batch_without_expiry_can_allocate():
    assert Batch("b1", "LAMP", 10, eta=None).can_allocate(OrderLine("o1", "LAMP", 1))


# --- domain rules -------------------------------------------------------------


def test_expired_batch_cannot_allocate():
    batch = Batch("b1", "LAMP", 10, eta=None, expiry=yesterday)
    assert not batch.can_allocate(OrderLine("o1", "LAMP", 1))


def test_batch_can_allocate_on_its_expiry_date():
    batch = Batch("b1", "LAMP", 10, eta=None, expiry=today)
    assert batch.can_allocate(OrderLine("o1", "LAMP", 1))


def test_allocation_skips_expired_batch_and_uses_the_next_one():
    expired = Batch("expired", "LAMP", 10, eta=None, expiry=yesterday)
    good = Batch("good", "LAMP", 10, eta=tomorrow)
    product = Product("LAMP", [expired, good])
    assert product.allocate(OrderLine("o1", "LAMP", 1)) == "good"
    assert expired.available_quantity == 10


def test_only_expired_stock_behaves_as_out_of_stock():
    product = Product("LAMP", [Batch("expired", "LAMP", 10, eta=None, expiry=yesterday)])
    assert product.allocate(OrderLine("o1", "LAMP", 1)) is None
    assert product.events[-1] == events.OutOfStock("LAMP")


# --- persistence through the service layer -------------------------------------


def test_expiry_is_saved_and_loaded_from_the_database(bus, load_batch):
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, None, later))
    assert load_batch("LAMP", "b1") == later


def test_missing_expiry_is_loaded_as_none(bus, load_batch):
    bus.handle(commands.CreateBatch("b1", "LAMP", 10))
    assert load_batch("LAMP", "b1") is None


# --- invalid values -------------------------------------------------------------


def test_expiry_before_eta_is_rejected_and_names_the_batch(bus):
    with pytest.raises(handlers.InvalidExpiry, match="b1"):
        bus.handle(commands.CreateBatch("b1", "LAMP", 10, later, tomorrow))


def test_expiry_that_is_not_a_date_is_rejected(bus):
    with pytest.raises(handlers.InvalidExpiry, match="b1"):
        bus.handle(commands.CreateBatch("b1", "LAMP", 10, None, "next week"))


def test_expiry_equal_to_eta_is_valid(bus, load_batch):
    bus.handle(commands.CreateBatch("b1", "LAMP", 10, later, later))
    assert load_batch("LAMP", "b1") == later
