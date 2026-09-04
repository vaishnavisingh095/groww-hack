import pytest
from pydantic import ValidationError

from app.models.watchlist import Watchlist


def test_valid_watchlist_is_accepted():
    wl = Watchlist(user_id="user123", instrument_ids=["abc", "def"])
    assert wl.user_id == "user123"
    assert wl.instrument_ids == ["abc", "def"]


def test_empty_instrument_list_is_allowed():
    """An empty watchlist is a valid state (plan.md: 'empty watchlist ->
    return empty list, not an error'), not a validation failure."""
    wl = Watchlist(user_id="user123")
    assert wl.instrument_ids == []


def test_missing_user_id_is_rejected():
    with pytest.raises(ValidationError):
        Watchlist()


def test_empty_user_id_is_rejected():
    with pytest.raises(ValidationError):
        Watchlist(user_id="")
