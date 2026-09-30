#!/usr/bin/env python3
"""
Warehouse Alcohol-Check Rush Simulation (multithreaded CLI)

Scenario
--------
Delayed bus(es) arrive at the same time -> rush at the gate. A guard's tablet
tracks every employee. One breathalyzer (1 min/test) checks each person:
  * NEGATIVE  -> allowed in, tablet updated
  * POSITIVE  -> sent home
  * INVALID   -> wait a few minutes (cooldown), then re-test
Real-world problems modelled:
  * Mouth residue: a person tested right after a POSITIVE can get a false positive.
    Mitigation: a positive that directly follows a positive is "suspect" -> re-test
    instead of rejecting.
  * Machine can give a wrong reading at random.
  * Frustrated waiters sneak in; the door sensor compares against the tablet and
    raises an alert that the guard acknowledges.
  * Machine can BREAK DOWN mid-queue (repair time; person keeps priority, no attempt lost).
  * Sent-home / supervisor-hold people may try to come back; door sensor catches them too.
  * Long waits (starvation): escalation priority, backup machine, ETA announcements.

Threads
-------
  Bus threads      - passengers arrive & enqueue
  Machine threads  - take people from the shared queue and test them
  Retest threads   - cooldown timer, then person re-joins queue with priority
  Monitor thread   - long-wait escalation, sneaking detection, backup machine, ETA

Run:  python3 warehouse_sim.py            (defaults = the interview scenario)
      python3 warehouse_sim.py --interactive
      python3 warehouse_sim.py --help
"""
import argparse
import random
import threading
import time
from dataclasses import dataclass, field

# ---- tablet statuses -------------------------------------------------------
OUTSIDE, WAITING, TESTING = "OUTSIDE", "WAITING", "TESTING"
COOLDOWN, ALLOWED, INSIDE = "COOLDOWN", "ALLOWED", "INSIDE"
SENT_HOME, MANUAL = "SENT_HOME", "MANUAL_REVIEW"
FINAL = {INSIDE, SENT_HOME, MANUAL}


@dataclass
class Employee:
    id: int
    name: str
    drunk: bool                      # ground truth (hidden from the guard)
    status: str = OUTSIDE
    first_arrival: float = 0.0
    enq_time: float = 0.0
    attempts: int = 0
    wait_total: float = 0.0
    escalated: bool = False
    strikes: int = 0
    suspect_used: bool = False      # residue re-check already granted
    clean_retest: bool = False      # next test uses a fresh mouthpiece
    history: list = field(default_factory=list)


class Clock:
    """Simulated minutes mapped to real seconds (scale = seconds per minute)."""

    def __init__(self, scale):
        self.scale, self.t0 = scale, time.time()

    def now(self):
        return (time.time() - self.t0) / self.scale

    def sleep(self, minutes):
        time.sleep(minutes * self.scale)


class WaitQueue:
    """Shared thread-safe queue. Escalated people (long-waiters, re-testers) go first;
    otherwise strictly first-come-first-served by original arrival time."""

    def __init__(self):
        self.cv = threading.Condition()
        self.items, self.closed = [], False

    def put(self, e):
        with self.cv:
            self.items.append(e)
            self.cv.notify()

    def get(self):
        with self.cv:
            while not self.items and not self.closed:
                self.cv.wait()
            if not self.items:
                return None
            e = min(self.items, key=lambda x: (0 if x.escalated else 1, x.first_arrival, x.id))
            self.items.remove(e)
            return e

    def waiting(self):
        with self.cv:
            return list(self.items)

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


class Simulation:
    def __init__(self, cfg):
        validate(cfg)
        self.cfg = cfg
        self.quiet = getattr(cfg, 'quiet', False)
        self.error = None
        self.timed_out = False
        self.clock = Clock(cfg.speed)
        self.q = WaitQueue()
        self.lock = threading.RLock()        # protects tablet data + stats
        self.log_lock = threading.Lock()
        self.done = threading.Event()
        self.employees = []
        self.resolved = 0
        self.incidents = []
        self.machines_started = 0
        self.threads = []
        self.stats = dict(tests=0, invalid=0, retests=0, suspect=0, false_pos=0,
                          wrong_reject=0, false_neg=0, contamination=0, escalations=0,
                          breakdowns=0)
        self.max_wait = 0.0

    # ---------------------------------------------------------------- utils
    def log(self, msg):
        if self.quiet:
            return
        with self.log_lock:
            print(f"[T+{self.clock.now():6.1f}m] {msg}", flush=True)

    def set_status(self, e, status, note=""):
        """The 'tablet': every status change is recorded here, thread-safely."""
        with self.lock:
            e.status = status
            e.history.append((round(self.clock.now(), 1), status, note))
            if status in FINAL:
                self.resolved += 1
                if self.resolved == len(self.employees):
                    self.done.set()

    def enqueue(self, e, priority=False):
        e.enq_time = self.clock.now()
        if priority:
            e.escalated = True
        self.set_status(e, WAITING)
        self.q.put(e)

    def spawn(self, target, *args):
        def guarded():
            try:
                target(*args)
            except Exception as ex:          # never let a crashed thread hang the run
                self.error = repr(ex)
                print(f"INTERNAL ERROR in thread {target.__name__}: {ex!r}", flush=True)
                self.done.set()
        t = threading.Thread(target=guarded, daemon=True)
        t.start()
        self.threads.append(t)
        return t

    # ----------------------------------------------------------------- buses
    def bus(self, bus_no, arrive_at, people):
        self.clock.sleep(max(0.0, arrive_at - self.clock.now()))
        self.log(f"BUS {bus_no} arrived with {len(people)} employees")
        for e in people:
            e.first_arrival = self.clock.now()
            self.enqueue(e)
        self.log(f"Guard tablet: {len(people)} from bus {bus_no} registered as WAITING "
                 f"(queue length now {len(self.q.waiting())})")

    # -------------------------------------------------------------- machines
    def start_machine(self, backup=False):
        with self.lock:
            self.machines_started += 1
            mid = self.machines_started
        self.log(f"Machine M{mid} {'(BACKUP) ' if backup else ''}is online")
        self.spawn(self.machine_loop, mid)

    def measure(self, e, prev_positive):
        """Return (result, contaminated)."""
        c = self.cfg
        if random.random() < c.invalid_rate:
            return "INVALID", False
        positive = e.drunk
        if random.random() < c.wrong_rate:          # machine error
            positive = not positive
        contaminated = False
        if (prev_positive and not positive and not e.clean_retest
                and random.random() < c.contamination):
            positive, contaminated = True, True      # residue from previous person
        return ("POSITIVE" if positive else "NEGATIVE"), contaminated

    def machine_loop(self, mid):
        prev_positive = False
        while True:
            e = self.q.get()
            if e is None:
                return
            now = self.clock.now()
            waited = now - e.enq_time
            with self.lock:
                e.wait_total += waited
                self.max_wait = max(self.max_wait, now - e.first_arrival)
            if random.random() < self.cfg.breakdown_rate:
                with self.lock:
                    self.stats["breakdowns"] += 1
                self.log(f"M{mid}: BREAKDOWN while loading {e.name} -> repair "
                         f"{self.cfg.repair_time}m, {e.name} back to queue with priority (no attempt used)")
                self.enqueue(e, priority=True)
                self.clock.sleep(self.cfg.repair_time)
                prev_positive = False            # machine cleaned during repair
                self.log(f"M{mid}: repaired, back online")
                continue
            with self.lock:
                e.attempts += 1
                self.stats["tests"] += 1
            self.set_status(e, TESTING, f"M{mid}")
            self.log(f"M{mid}: testing {e.name} (attempt {e.attempts}, waited {waited:.1f}m this round)")
            self.clock.sleep(self.cfg.test_time)
            result, contaminated = self.measure(e, prev_positive)
            was_prev_positive, prev_positive = prev_positive, (result == "POSITIVE")
            self.handle(e, result, was_prev_positive, contaminated, mid)

    def handle(self, e, result, prev_positive, contaminated, mid):
        with self.lock:
            if contaminated:
                self.stats["contamination"] += 1
        if result == "NEGATIVE":
            if e.drunk:
                with self.lock:
                    self.stats["false_neg"] += 1
                self.log(f"M{mid}: {e.name} NEGATIVE (machine missed alcohol - known limitation)")
            else:
                self.log(f"M{mid}: {e.name} NEGATIVE -> allowed")
            self.set_status(e, ALLOWED)
            self.admit(e)
        elif result == "POSITIVE":
            # Mitigation for mouth residue: positive right after positive => confirm first.
            if prev_positive and not e.suspect_used:
                e.suspect_used = True
                with self.lock:
                    self.stats["suspect"] += 1
                if e.attempts >= self.cfg.max_attempts:
                    self.set_status(e, MANUAL, "suspect residue, no attempts left")
                    self.log(f"M{mid}: {e.name} POSITIVE (possible residue) and no attempts left "
                             f"-> supervisor MANUAL REVIEW instead of unfair rejection")
                else:
                    e.clean_retest = True
                    self.log(f"M{mid}: {e.name} POSITIVE but previous person was POSITIVE -> "
                             f"possible residue, SUSPECT, re-test in {self.cfg.retest_wait}m "
                             f"with fresh mouthpiece")
                    self.retest(e)
            else:
                if not e.drunk:
                    with self.lock:
                        self.stats["wrong_reject"] += 1
                self.set_status(e, SENT_HOME, "alcohol detected")
                self.log(f"M{mid}: {e.name} POSITIVE -> SENT HOME")
        else:  # INVALID
            with self.lock:
                self.stats["invalid"] += 1
            if e.attempts >= self.cfg.max_attempts:
                self.set_status(e, MANUAL, "too many failed readings")
                self.log(f"M{mid}: {e.name} failed {e.attempts} times -> supervisor MANUAL REVIEW")
            else:
                self.log(f"M{mid}: {e.name} reading INVALID -> cooldown {self.cfg.retest_wait}m then re-test")
                self.retest(e)

    def retest(self, e):
        with self.lock:
            self.stats["retests"] += 1
        self.set_status(e, COOLDOWN)
        self.spawn(self._retest_timer, e)

    def _retest_timer(self, e):
        self.clock.sleep(self.cfg.retest_wait)
        self.log(f"{e.name} cooldown over -> back in queue with PRIORITY (already waited long)")
        self.enqueue(e, priority=True)

    # ------------------------------------------------------------------ gate
    def admit(self, e):
        """Gate opens only if the tablet says ALLOWED; tablet is then updated."""
        with self.lock:
            ok = e.status == ALLOWED
        if ok:
            self.set_status(e, INSIDE, "entered via gate")
            self.log(f"GATE ACK: {e.name} entered. Tablet updated -> INSIDE")

    def sneak(self, e):
        """A frustrated (or barred) person slips in. Door sensor sees entry, tablet says otherwise."""
        with self.lock:
            if e.status not in (WAITING, COOLDOWN, SENT_HOME, MANUAL):
                return
            e.strikes += 1
            shown = e.status
            self.incidents.append((round(self.clock.now(), 1), e.name, shown))
            n = len(self.incidents)
        self.log(f"!!! ALERT: door sensor - {e.name} entered warehouse but tablet shows {shown} !!!")
        if shown in (WAITING, COOLDOWN):
            tail = "place in queue kept"
        else:
            tail = "remains barred for today"
        self.log(f"GUARD ACK: incident #{n} logged, {e.name} escorted out, {tail}, supervisor notified")

    # --------------------------------------------------------------- monitor
    def monitor(self):
        c = self.cfg
        next_eta = 5
        while not self.done.is_set():
            self.clock.sleep(1)
            now = self.clock.now()
            waiting = self.q.waiting()
            # 1) long-wait escalation (aging) - prevents starvation
            for e in waiting:
                if now - e.first_arrival > c.patience and not e.escalated:
                    e.escalated = True
                    with self.lock:
                        self.stats["escalations"] += 1
                    self.log(f"WAIT ALERT: {e.name} waiting {now - e.first_arrival:.0f}m -> "
                             f"moved to priority lane")
            # 2) frustrated people may sneak in
            with self.lock:
                frustrated = [e for e in self.employees
                              if e.status in (WAITING, COOLDOWN) and e.strikes == 0
                              and now - e.first_arrival > c.patience]
            for e in frustrated:
                if random.random() < c.sneak_rate:
                    self.sneak(e)
            with self.lock:
                barred = [e for e in self.employees
                          if e.status in (SENT_HOME, MANUAL) and e.strikes == 0]
            for e in barred:                      # sent-home people trying to return
                if random.random() < c.return_rate:
                    self.sneak(e)
            # 3) queue too slow -> open backup machine
            worst = max((now - e.first_arrival for e in waiting), default=0)
            with self.lock:
                can_add = self.machines_started < 1 + c.backup_machines
            if can_add and worst > c.patience * 1.5:
                self.log(f"Manager: longest wait {worst:.0f}m exceeds limit -> deploying backup machine")
                self.start_machine(backup=True)
            # 4) ETA announcements
            if now >= next_eta and waiting:
                eta = len(waiting) * c.test_time / max(1, self.machines_started)
                self.log(f"ANNOUNCEMENT: {len(waiting)} in queue, estimated wait ~{eta:.0f} min")
                next_eta += 5

    # ------------------------------------------------------------------- run
    def run(self):
        c = self.cfg
        random.seed(c.seed)
        if c.workers == 0:
            print("No workers scheduled - nothing to simulate.")
            return
        self.employees = [Employee(i + 1, f"Emp{i + 1:02d}", random.random() < c.alcohol_rate)
                          for i in range(c.workers)]
        print("=" * 70)
        print(f"WAREHOUSE SIMULATION: {c.workers} workers, {c.buses} buses, test={c.test_time}m, "
              f"retest wait={c.retest_wait}m")
        print(f"invalid={c.invalid_rate:.0%} wrong={c.wrong_rate:.0%} alcohol={c.alcohol_rate:.0%} "
              f"residue={c.contamination:.0%} patience={c.patience}m backup={c.backup_machines}")
        print("=" * 70)

        # split workers across buses; bus 1 is delayed so it lands with the last bus -> rush
        chunks = [self.employees[i::c.buses] for i in range(c.buses)]
        arrivals = [i * c.bus_gap + (c.bus_delay if i == 0 else 0) for i in range(c.buses)]
        if c.bus_delay:
            self.log(f"Bus 1 is DELAYED by {c.bus_delay}m")
        if len(set(round(a, 3) for a in arrivals)) < len(arrivals):
            self.log("Multiple buses will arrive together -> RUSH at the gate")

        self.start_machine()
        self.spawn(self.monitor)
        for i, (a, ch) in enumerate(zip(arrivals, chunks), 1):
            self.spawn(self.bus, i, a, ch)

        if not self.done.wait(timeout=c.max_minutes * c.speed):
            self.timed_out = True
            print(f"WATCHDOG: simulation exceeded {c.max_minutes} simulated minutes - stopping.")
        self.q.close()
        for t in self.threads:
            t.join(timeout=2)
        self.report()

    def report(self):
        inside = [e for e in self.employees if e.status == INSIDE]
        home = [e for e in self.employees if e.status == SENT_HOME]
        manual = [e for e in self.employees if e.status == MANUAL]
        avg = sum(e.wait_total for e in self.employees) / len(self.employees)
        s = self.stats
        print("\n" + "=" * 70)
        print(f"FINAL REPORT  (total simulated time: {self.clock.now():.1f} min)")
        print("=" * 70)
        print(f"Entered warehouse      : {len(inside)}")
        print(f"Sent home (alcohol)    : {len(home)}")
        print(f"Supervisor review      : {len(manual)}")
        print(f"Average queue wait     : {avg:.1f} min   | longest total wait: {self.max_wait:.1f} min")
        print(f"Tests run / re-tests   : {s['tests']} / {s['retests']}   (invalid readings: {s['invalid']})")
        print(f"Residue false positives: {s['contamination']}   suspect re-tests: {s['suspect']}")
        print(f"Sober people wrongly sent home: {s['wrong_reject']}   | drunk missed by machine: {s['false_neg']}")
        print(f"Long-wait escalations  : {s['escalations']}   | machines used: {self.machines_started}")
        print(f"Machine breakdowns     : {s['breakdowns']}")
        unresolved = [e for e in self.employees if e.status not in FINAL]
        if unresolved:
            print(f"UNRESOLVED (watchdog)  : {len(unresolved)}")
        print(f"Security incidents (entered while tablet != ALLOWED): {len(self.incidents)}")
        for t, n, st in self.incidents:
            print(f"   T+{t}m  {n} (tablet showed {st})")
        print("=" * 70)


def validate(c):
    errs = []
    for k in ("invalid_rate", "wrong_rate", "alcohol_rate", "contamination", "sneak_rate", "return_rate"):
        if not 0 <= getattr(c, k) <= 1:
            errs.append(f"{k} must be between 0 and 1")
    if not 0 <= c.breakdown_rate < 1:
        errs.append("breakdown_rate must be >= 0 and < 1")
    for k in ("workers", "test_time", "retest_wait", "bus_delay", "bus_gap",
              "backup_machines", "repair_time"):
        if getattr(c, k) < 0:
            errs.append(f"{k} must not be negative")
    if c.buses < 1:
        errs.append("buses must be at least 1")
    if c.max_attempts < 1:
        errs.append("max_attempts must be at least 1")
    if c.patience <= 0:
        errs.append("patience must be greater than 0")
    if c.speed <= 0:
        errs.append("speed must be greater than 0")
    if errs:
        raise ValueError("; ".join(errs))


def build_parser():
    p = argparse.ArgumentParser(description="Warehouse alcohol-check rush simulation")
    a = p.add_argument
    a("--workers", type=int, default=40, help="number of employees (default 40)")
    a("--buses", type=int, default=2, help="number of buses (default 2)")
    a("--bus-delay", type=float, default=5, help="minutes bus 1 is delayed (default 5)")
    a("--bus-gap", type=float, default=5, help="scheduled gap between buses (default 5)")
    a("--test-time", type=float, default=1, help="minutes per test (default 1)")
    a("--retest-wait", type=float, default=5, help="cooldown before re-test (default 5)")
    a("--invalid-rate", type=float, default=0.10, help="P(failed/invalid reading)")
    a("--wrong-rate", type=float, default=0.03, help="P(machine flips the result)")
    a("--alcohol-rate", type=float, default=0.10, help="P(employee has been drinking)")
    a("--contamination", type=float, default=0.35, help="P(residue false positive after a positive)")
    a("--patience", type=float, default=12, help="minutes before a waiter is 'frustrated'")
    a("--sneak-rate", type=float, default=0.02, help="per-minute P(frustrated person sneaks in)")
    a("--backup-machines", type=int, default=1, help="extra machines manager may deploy")
    a("--max-attempts", type=int, default=3, help="tests before supervisor review")
    a("--breakdown-rate", type=float, default=0.02, help="P(machine breaks down per test), <1")
    a("--repair-time", type=float, default=3, help="minutes to repair a breakdown")
    a("--return-rate", type=float, default=0.02, help="per-minute P(sent-home person tries to return)")
    a("--max-minutes", type=float, default=2000, help="watchdog limit in simulated minutes")
    a("--speed", type=float, default=0.1, help="real seconds per simulated minute")
    a("--seed", type=int, default=None, help="random seed for reproducible runs")
    a("--interactive", action="store_true", help="prompt for the main inputs")
    return p


def parse_args():
    p = build_parser()
    args = p.parse_args()
    if args.interactive:
        def ask(label, key, cast):
            while True:
                try:
                    v = input(f"{label} [{getattr(args, key)}]: ").strip()
                except EOFError:
                    return
                if not v:
                    return
                try:
                    setattr(args, key, cast(v))
                    return
                except ValueError:
                    print("  Please enter a valid number.")
        ask("How many workers", "workers", int)
        ask("How many minutes per test", "test_time", float)
        ask("Invalid-sample probability (0-1)", "invalid_rate", float)
        ask("Machine wrong-result probability (0-1)", "wrong_rate", float)
        ask("Re-test waiting time (min)", "retest_wait", float)
        ask("Patience before escalation (min)", "patience", float)
    try:
        validate(args)
    except ValueError as ex:
        p.error(str(ex))
    return args


if __name__ == "__main__":
    Simulation(parse_args()).run()
