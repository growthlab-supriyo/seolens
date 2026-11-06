"""How many pages to fetch at the same time: AIMD (additive increase, multiplicative decrease), like TCP.

The old desktop crawler fetched pages in batches and measured the slowest page of each batch: one slow page made the
whole batch "slow", the crawler dropped from 12 to 2 pages at a time and never sped up again. This controller instead:

- looks at the median time of the last pages (a few slow pages don't count),
- slows down gradually (x0.7) only when the server stays slow, at most once per cool-down period,
- halves at once on a real "too many requests" signal (HTTP 429 / 503),
- speeds up again by one page at a time after a run of healthy answers,
- never goes below a minimum (unless robots.txt asks for a Crawl-delay) or above a maximum.
"""
import statistics
import threading
import time
from collections import deque

SPEEDS = {
    # name: (start, minimum, maximum)
    "Polite": (4, 2, 8),
    "Medium": (10, 4, 20),
    "Fast": (20, 6, 40),
}


class Throttle:
    def __init__(self, start=10, minimum=4, maximum=20, slow_s=5.0, window=20, cooldown_s=15.0, clock=time.monotonic):
        self.minimum, self.maximum = minimum, maximum
        self.current = max(minimum, min(start, maximum))
        self.slow_s = slow_s
        self.window = window
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.latencies = deque(maxlen=window)
        self.healthy = 0
        self.last_change = clock()
        self.paused_until = 0.0
        self.slowdowns = 0
        self.reason = ""
        self._lock = threading.Lock()

    def fixed(self, n):
        """robots.txt Crawl-delay: always n pages at a time."""
        self.current = self.minimum = self.maximum = n

    def median(self):
        return statistics.median(self.latencies) if self.latencies else 0.0

    def record(self, seconds, ok=True):
        """One answer: how long it took, and whether it was a normal answer (not an error / refusal)."""
        with self._lock:
            now = self.clock()
            self.latencies.append(seconds)
            full = len(self.latencies) >= max(5, self.window // 2)
            med = self.median()
            if full and med > self.slow_s:
                self.healthy = 0
                if now - self.last_change >= self.cooldown_s and self.current > self.minimum:
                    self.current = max(self.minimum, int(self.current * 0.7))
                    self.last_change = now
                    self.slowdowns += 1
                    self.reason = "the server stayed slow (median %.1f s per page)" % med
                    self.latencies.clear()
                    return "down"
                return None
            if not ok:
                self.healthy = 0
                return None
            self.healthy += 1
            if self.healthy >= self.current and full and med < self.slow_s * 0.6 and self.current < self.maximum \
                    and now - self.last_change >= self.cooldown_s / 3:
                self.current += 1
                self.healthy = 0
                self.last_change = now
                return "up"
            return None

    def overload(self, retry_after=0.0, why="HTTP 429"):
        """The server said it is overloaded or rate-limiting: halve and pause for Retry-After (2-60 s)."""
        with self._lock:
            now = self.clock()
            if now - self.last_change >= 2:
                self.current = max(self.minimum, self.current // 2)
                self.last_change = now
                self.slowdowns += 1
            self.reason = "the site asked the crawler to slow down (%s)" % why
            self.healthy = 0
            self.latencies.clear()
            self.paused_until = max(self.paused_until, now + min(60.0, max(2.0, retry_after or 0)))

    def wait_s(self):
        return max(0.0, self.paused_until - self.clock())
