"""
MongoDB connection management.

This module owns exactly one thing: producing a usable pymongo Database
handle. It does not know about Instrument, Watchlist, or any other domain
concept — that separation is what lets domain/model code be imported and
unit-tested without ever touching this module or a real MongoDB server.
"""
from pymongo import MongoClient
from pymongo.database import Database

from app.config import settings

_client: MongoClient | None = None


def get_client() -> MongoClient:
    """
    Return a shared MongoClient, creating it on first use.

    A single shared client (not one per request) is the standard pattern
    for pymongo: the client already manages its own connection pool
    internally, so creating a new client per request would just add
    connection-setup overhead without any benefit.
    """
    global _client
    if _client is None:
        _client = MongoClient(settings.mongodb_uri)
    return _client


def get_database() -> Database:
    """Return the application's database handle."""
    return get_client()[settings.mongodb_db_name]


def close_client() -> None:
    """Close the shared client. Used on app shutdown and in test teardown."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
