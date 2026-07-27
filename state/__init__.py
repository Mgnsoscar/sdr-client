"""
State package — background polling, log tailing, and small client-side helpers.

Public surface:
    from state import Poller, FastSnapshot, SlowSnapshot
    from state import LogTailer
    from state import local_ip
"""
from .poller import Poller, FastSnapshot, SlowSnapshot
from .log_tail import LogTailer
from .netutil import local_ip

__all__ = ["Poller", "FastSnapshot", "SlowSnapshot", "LogTailer", "local_ip"]