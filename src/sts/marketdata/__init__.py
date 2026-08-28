"""Shared market-data singleton: wraps data.live.LivePoller + daily-bar cache +
freshness tracking + fan-out of completed-bar events to per-session queues."""
from sts.marketdata.service import MarketDataService

__all__ = ["MarketDataService"]
