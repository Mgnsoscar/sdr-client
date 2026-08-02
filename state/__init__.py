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
from .plan_store import PlanStore, new_plan_id
from .schedule_store import ScheduleStore, new_scheduled_id
from .library_store import LibraryStore
from .library_sync import pull_library, diff_library, LibraryDiff
from .library_client import LibraryClient, LibraryError

__all__ = ["Poller", "FastSnapshot", "SlowSnapshot", "LogTailer", "local_ip",
           "PlanStore", "new_plan_id", "ScheduleStore", "new_scheduled_id",
           "LibraryStore", "pull_library", "diff_library", "LibraryDiff",
           "LibraryClient", "LibraryError"]