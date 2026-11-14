"""审核调度与录入共用口径；时间窗不限制比赛是赛前还是滚球。"""

import os


def fresh_priority_window_seconds() -> int:
    return max(60, min(3600, int(os.environ.get('REVIEW_FRESH_PRIORITY_SECONDS', '300'))))


def realtime_window_seconds() -> int:
    return max(60,min(604800,int(os.environ.get('REVIEW_REALTIME_WINDOW_SECONDS','86400'))))


REALTIME_WINDOW_SECONDS = realtime_window_seconds()
FRESH_PRIORITY_WINDOW_SECONDS = min(fresh_priority_window_seconds(), REALTIME_WINDOW_SECONDS)
