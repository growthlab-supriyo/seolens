from simplescope.throttle import Throttle


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_a_few_slow_pages_do_not_slow_the_crawl():
    """The old crawler dropped 12 → 2 when one batch took > 4 s. Here a few slow pages among fast ones change nothing."""
    clock = Clock()
    t = Throttle(start=10, minimum=4, maximum=20, clock=clock)
    for i in range(200):
        clock.t += 0.1
        t.record(9.0 if i % 10 == 0 else 0.4)
    assert t.current >= 10
    assert t.slowdowns == 0


def test_sustained_slowness_reduces_gradually_and_never_below_minimum():
    clock = Clock()
    t = Throttle(start=10, minimum=4, maximum=20, clock=clock)
    seen = []
    for _ in range(400):
        clock.t += 1.0
        t.record(8.0)
        seen.append(t.current)
    assert seen[-1] == 4
    # x0.7 steps, at most one per cool-down: 10 → 7 → 4, never a jump straight to the minimum
    steps = sorted(set(seen), reverse=True)
    assert steps == [10, 7, 4]


def test_speeds_up_again_when_the_server_recovers():
    clock = Clock()
    t = Throttle(start=10, minimum=4, maximum=20, clock=clock)
    for _ in range(100):
        clock.t += 1.0
        t.record(8.0)
    low = t.current
    for _ in range(2000):
        clock.t += 0.2
        t.record(0.3)
    assert t.current > low
    assert t.current == 20


def test_429_halves_and_pauses_for_retry_after():
    clock = Clock()
    t = Throttle(start=10, minimum=2, maximum=20, clock=clock)
    clock.t = 100
    t.overload(retry_after=7)
    assert t.current == 5
    assert abs(t.wait_s() - 7) < 1e-9
    clock.t += 7
    assert t.wait_s() == 0


def test_crawl_delay_fixes_concurrency_to_one():
    t = Throttle(start=10, minimum=4, maximum=20)
    t.fixed(1)
    for _ in range(100):
        t.record(0.1)
    assert t.current == 1
