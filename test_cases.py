#!/usr/bin/env python3
"""
Runs the warehouse simulation through every edge case and checks invariants.
Usage:  python3 test_cases.py
"""
import sys
import threading
import time

from warehouse_sim import (ALLOWED, FINAL, INSIDE, MANUAL, SENT_HOME, Simulation,
                           build_parser, validate)

FAST = dict(speed=0.02, quiet=True, max_minutes=3000)


def make_cfg(**over):
    cfg = build_parser().parse_args([])
    for k, v in {**FAST, **over}.items():
        setattr(cfg, k, v)
    return cfg


def run(**over):
    sim = Simulation(make_cfg(**over))
    sim.run()
    return sim


def check_invariants(sim):
    """Rules that must hold for ANY configuration."""
    c = sim.cfg
    assert sim.error is None, f"thread crashed: {sim.error}"
    assert not sim.timed_out, "simulation hung / watchdog fired"
    assert all(e.status in FINAL for e in sim.employees), "someone left unresolved"
    inside = [e for e in sim.employees if e.status == INSIDE]
    home = [e for e in sim.employees if e.status == SENT_HOME]
    manual = [e for e in sim.employees if e.status == MANUAL]
    assert len(inside) + len(home) + len(manual) == c.workers, "people lost or duplicated"
    for e in inside:     # nobody is INSIDE unless the tablet said ALLOWED first
        seq = [h[1] for h in e.history]
        assert ALLOWED in seq and seq.index(ALLOWED) < seq.index(INSIDE), f"{e.name} entered without ALLOWED"
    for e in sim.employees:
        assert e.attempts <= c.max_attempts, f"{e.name} tested too many times"
        assert e.strikes <= 1, "same person logged twice"
        assert e.wait_total >= 0
        assert sum(1 for h in e.history if h[1] == INSIDE) <= 1, "entered twice"
    assert len(sim.incidents) == sum(e.strikes for e in sim.employees), "incident log mismatch"
    for _, _, shown in sim.incidents:
        assert shown not in (ALLOWED, INSIDE), "false alarm on legitimate entry"
    wrong = sum(1 for e in home if not e.drunk)
    assert wrong == sim.stats["wrong_reject"], "wrong-reject counter mismatch"
    assert sim.machines_started <= 1 + c.backup_machines, "too many machines"
    assert not any(t.is_alive() for t in sim.threads), "thread leak"
    return inside, home, manual


RESULTS = []


def case(name):
    def deco(fn):
        def wrapper():
            t0 = time.time()
            try:
                msg = fn() or ""
                RESULTS.append((name, "PASS", msg, time.time() - t0))
            except Exception as ex:          # noqa
                RESULTS.append((name, "FAIL", repr(ex), time.time() - t0))
        wrapper.__name__ = fn.__name__
        CASES.append(wrapper)
        return wrapper
    return deco


CASES = []


@case("01 default interview scenario (40 workers, 2 buses, delay)")
def _():
    s = run(seed=1)
    i, h, m = check_invariants(s)
    return f"inside={len(i)} home={len(h)} manual={len(m)} incidents={len(s.incidents)}"


@case("02 single worker")
def _():
    s = run(workers=1, buses=1, seed=2)
    check_invariants(s)


@case("03 zero workers exits cleanly (no hang)")
def _():
    s = Simulation(make_cfg(workers=0))
    s.run()
    assert s.employees == []


@case("04 fewer workers than buses (empty buses)")
def _():
    s = run(workers=2, buses=5, seed=4)
    check_invariants(s)


@case("05 single bus, no delay (no rush)")
def _():
    s = run(buses=1, bus_delay=0, seed=5)
    check_invariants(s)


@case("06 perfect machine, nobody drinks -> everyone enters")
def _():
    s = run(alcohol_rate=0, invalid_rate=0, wrong_rate=0, contamination=0, breakdown_rate=0, seed=6)
    i, h, m = check_invariants(s)
    assert len(i) == 40 and not h and not m


@case("07 perfect machine, everybody drinks -> everyone sent home")
def _():
    s = run(alcohol_rate=1, invalid_rate=0, wrong_rate=0, contamination=0, breakdown_rate=0, seed=7)
    i, h, m = check_invariants(s)
    assert len(h) == 40 and not i


@case("08 perfect machine, mixed -> exact split by ground truth")
def _():
    s = run(alcohol_rate=0.3, invalid_rate=0, wrong_rate=0, contamination=0, breakdown_rate=0, seed=8)
    i, h, m = check_invariants(s)
    assert all(not e.drunk for e in i) and all(e.drunk for e in h)


@case("09 every reading invalid -> all go to supervisor after max attempts")
def _():
    s = run(invalid_rate=1, breakdown_rate=0, workers=15, seed=9)
    i, h, m = check_invariants(s)
    assert len(m) == 15 and all(e.attempts == s.cfg.max_attempts for e in m)


@case("10 max_attempts=1: first invalid goes straight to supervisor")
def _():
    s = run(invalid_rate=0.5, max_attempts=1, breakdown_rate=0, seed=10)
    check_invariants(s)


@case("11 residue always contaminates: no sober person wrongly rejected")
def _():
    s = run(alcohol_rate=0.5, contamination=1, invalid_rate=0, wrong_rate=0, breakdown_rate=0,
            workers=40, seed=11)
    i, h, m = check_invariants(s)
    assert s.stats["wrong_reject"] == 0, "suspect re-test mitigation failed"
    assert s.stats["contamination"] > 0, "scenario never triggered residue"
    assert all(not e.drunk for e in i)
    return f"residue hits={s.stats['contamination']} suspect retests={s.stats['suspect']}"


@case("12 residue + invalid readings + max_attempts=2 (suspect can run out of attempts)")
def _():
    s = run(alcohol_rate=0.5, contamination=1, invalid_rate=0.4, max_attempts=2, breakdown_rate=0,
            seed=12)
    check_invariants(s)


@case("13 machine wrong every time (wrong_rate=1)")
def _():
    s = run(wrong_rate=1, invalid_rate=0, contamination=0, breakdown_rate=0, seed=13)
    check_invariants(s)


@case("14 everybody sneaks: door sensor alerts, nobody bypasses the tablet")
def _():
    s = run(sneak_rate=1, patience=3, return_rate=1, seed=14)
    check_invariants(s)
    assert len(s.incidents) > 0
    return f"incidents={len(s.incidents)}"


@case("15 sent-home people try to return and are caught")
def _():
    s = run(alcohol_rate=0.6, return_rate=1, sneak_rate=0, invalid_rate=0, breakdown_rate=0,
            seed=15)
    check_invariants(s)
    assert any(shown in (SENT_HOME, MANUAL) for _, _, shown in s.incidents)


@case("16 starvation: 120 workers, tiny patience, no backup -> escalations, all resolved")
def _():
    s = run(workers=120, patience=4, backup_machines=0, sneak_rate=0, seed=16)
    check_invariants(s)
    assert s.stats["escalations"] > 0 and s.machines_started == 1
    return f"escalations={s.stats['escalations']} longest wait={s.max_wait:.0f}m"


@case("17 backup machines deployed under heavy rush")
def _():
    s = run(workers=120, patience=4, backup_machines=3, sneak_rate=0, seed=17)
    check_invariants(s)
    assert s.machines_started > 1
    return f"machines used={s.machines_started}"


@case("18 backup machines improve the longest wait vs none")
def _():
    a = run(workers=100, patience=5, backup_machines=0, sneak_rate=0, breakdown_rate=0,
            invalid_rate=0, seed=18)
    b = run(workers=100, patience=5, backup_machines=3, sneak_rate=0, breakdown_rate=0,
            invalid_rate=0, seed=18)
    check_invariants(a)
    check_invariants(b)
    assert b.max_wait < a.max_wait, (a.max_wait, b.max_wait)
    return f"longest wait {a.max_wait:.0f}m -> {b.max_wait:.0f}m"


@case("19 frequent machine breakdowns (50%) still finishes, nobody loses an attempt")
def _():
    s = run(breakdown_rate=0.5, workers=25, seed=19)
    i, h, m = check_invariants(s)
    assert s.stats["breakdowns"] > 0
    return f"breakdowns={s.stats['breakdowns']}"


@case("20 zero test time and zero retest wait")
def _():
    s = run(test_time=0, retest_wait=0, seed=20)
    check_invariants(s)


@case("21 many buses (6) arriving together, 150 workers")
def _():
    s = run(workers=150, buses=6, bus_gap=0, bus_delay=0, seed=21, speed=0.01)
    check_invariants(s)


@case("22 worst-case chaos: everything high at once")
def _():
    s = run(workers=60, alcohol_rate=0.4, invalid_rate=0.4, wrong_rate=0.2, contamination=0.8,
            sneak_rate=0.5, return_rate=0.5, breakdown_rate=0.3, patience=3, seed=22)
    check_invariants(s)


@case("23 race-condition stress: 25 random runs at very high speed")
def _():
    for seed in range(100, 125):
        s = run(workers=40, seed=seed, speed=0.004, sneak_rate=0.3, breakdown_rate=0.1)
        check_invariants(s)
    return "25/25 clean"


@case("24 invalid inputs are rejected with clear errors")
def _():
    bad = [dict(workers=-1), dict(buses=0), dict(speed=0), dict(invalid_rate=1.5),
           dict(wrong_rate=-0.1), dict(patience=0), dict(max_attempts=0),
           dict(breakdown_rate=1), dict(test_time=-2), dict(retest_wait=-1)]
    for b in bad:
        try:
            validate(make_cfg(**b))
        except ValueError:
            continue
        raise AssertionError(f"accepted bad input {b}")
    return f"{len(bad)} bad inputs rejected"


@case("25 thread crash is reported instead of hanging")
def _():
    s = Simulation(make_cfg(workers=5, seed=25))
    orig = s.measure
    def boom(*a, **k):
        raise RuntimeError("simulated machine crash")
    s.measure = boom
    s.run()
    assert s.error is not None and "crash" in s.error


def main():
    t0 = time.time()
    for fn in CASES:
        fn()
    width = max(len(r[0]) for r in RESULTS)
    print("\n" + "=" * (width + 30))
    for name, status, msg, dt in RESULTS:
        print(f"{status}  {name:<{width}}  {dt:4.1f}s  {msg}")
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print("=" * (width + 30))
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed in {time.time() - t0:.0f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
