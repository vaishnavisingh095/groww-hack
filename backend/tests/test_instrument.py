import pytest
from pydantic import ValidationError

from app.models.instrument import Instrument, Exchange


def test_valid_instrument_is_accepted():
    inst = Instrument(symbol="RELIANCE", exchange=Exchange.NSE)
    assert inst.symbol == "RELIANCE"
    assert inst.exchange == Exchange.NSE


def test_symbol_is_normalized_to_uppercase():
    inst = Instrument(symbol="reliance", exchange=Exchange.NSE)
    assert inst.symbol == "RELIANCE"


def test_symbol_is_stripped_of_surrounding_whitespace():
    inst = Instrument(symbol="  TCS  ", exchange=Exchange.NSE)
    assert inst.symbol == "TCS"


def test_empty_symbol_is_rejected():
    with pytest.raises(ValidationError):
        Instrument(symbol="", exchange=Exchange.NSE)


def test_whitespace_only_symbol_is_rejected():
    with pytest.raises(ValidationError):
        Instrument(symbol="   ", exchange=Exchange.NSE)


def test_missing_symbol_is_rejected():
    with pytest.raises(ValidationError):
        Instrument(exchange=Exchange.NSE)


def test_missing_exchange_is_rejected():
    with pytest.raises(ValidationError):
        Instrument(symbol="RELIANCE")


def test_invalid_exchange_value_is_rejected():
    with pytest.raises(ValidationError):
        Instrument(symbol="RELIANCE", exchange="LSE")


def test_bse_exchange_is_accepted():
    inst = Instrument(symbol="RELIANCE", exchange=Exchange.BSE)
    assert inst.exchange == Exchange.BSE


def test_company_name_is_optional():
    inst = Instrument(symbol="RELIANCE", exchange=Exchange.NSE)
    assert inst.company_name is None


def test_created_at_defaults_to_now():
    inst = Instrument(symbol="RELIANCE", exchange=Exchange.NSE)
    assert inst.created_at is not None
