"""Generic WS/SSE stream channel package."""

from coworker.channels.stream.channel import StreamChannel
from coworker.channels.stream.connection_pool import ConnectionPool
from coworker.channels.stream.registration import RegistrationStore
from coworker.channels.stream.runtime import StreamRuntime
from coworker.channels.stream.wire import SHUTDOWN_SENTINEL, serialize_outbound_message

__all__ = [
    "ConnectionPool",
    "RegistrationStore",
    "SHUTDOWN_SENTINEL",
    "StreamChannel",
    "StreamRuntime",
    "serialize_outbound_message",
]
