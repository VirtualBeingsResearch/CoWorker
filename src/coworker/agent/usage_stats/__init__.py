"""Usage statistics package.

The public surface is :class:`UsageStatsCollector`; bucket schema, pricing,
report shaping and persistence live in their own modules.
"""

from .collector import UsageStatsCollector

__all__ = ["UsageStatsCollector"]
