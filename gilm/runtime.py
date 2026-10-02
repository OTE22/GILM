"""Bounded, per-process admission and authenticated-scope request limits."""

import time
from collections import defaultdict, deque


class Governor:
    def __init__(self, settings):
        self.settings = settings
        self.active = 0
        self.windows = defaultdict(deque)

    def enter(self):
        if self.active >= self.settings.max_inflight_requests:
            return False
        self.active += 1
        return True

    def leave(self):
        self.active -= 1

    def allow(self, identity):
        # Identities come only from startup configuration, never arbitrary headers.
        window = self.windows[(identity.tenant, identity.scope)]
        now = time.monotonic()
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= self.settings.requests_per_minute:
            return False
        window.append(now)
        return True
