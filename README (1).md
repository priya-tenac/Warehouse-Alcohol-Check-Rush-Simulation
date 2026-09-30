# Warehouse Alcohol-Check Rush Simulation

A multithreaded warehouse simulation where employees arrive by bus, queue for an alcohol check, and either enter the facility or are sent home. The project includes a browser dashboard for viewing the most important operational metrics in a simple, easy-to-read format.

This project simulates:
- delayed and overlapping bus arrivals
- a single shared queue with priority escalation
- re-testing and manual review flow
- residue contamination and false results
- backup machine deployment during long waits
- door-sensor incidents when someone enters against tablet status

## Features

- Python simulation engine with real concurrency using threads
- CLI runner for scenario testing and experimentation
- Browser dashboard for live KPI overview
- Full test suite covering edge cases and invariants

## Project files

- `warehouse_sim.py` — core simulation logic
- `test_cases.py` — 25 edge case validations
- `dashboard.html` — browser dashboard UI
- `dashboard_server.py` — local server for running the dashboard
- `README (1).md` — project documentation

## Quick start

### 1) Run the simulation in the terminal

```bash
python warehouse_sim.py
```

Optional arguments:

```bash
python warehouse_sim.py --workers 100 --buses 3 --bus-delay 0
python warehouse_sim.py --interactive
python warehouse_sim.py --seed 7 --speed 0.3
python warehouse_sim.py --help
```

### 2) Run the dashboard

```bash
python dashboard_server.py
```

Then open:

```text
http://127.0.0.1:8000/
```

The dashboard lets you adjust key inputs such as worker count, bus delay, invalid reading rate, and backup machine count, then rerun the simulation and view results in a simple operations dashboard.

### 3) Run the test suite

```bash
python test_cases.py
```

## Scenario details

- One machine handles testing by default, with optional backup machines added during long waits.
- Employees wait in a shared queue.
- Long-waiting employees are escalated to a priority lane.
- Invalid or suspect readings trigger re-testing after a cooldown.
- Residue on a mouthpiece can create false positives for the next person tested.
- Frustrated or barred employees may try to sneak in, which triggers a security incident.
- The simulation records all incidents, wait times, and final warehouse outcomes.

## Example final report

```text
======================================================================
FINAL REPORT  (total simulated time: 35.2 min)
======================================================================
Entered warehouse      : 35
Sent home (alcohol)    : 5
Supervisor review      : 0
Average queue wait     : 16.6 min   | longest total wait: 28.2 min
Tests run / re-tests   : 40 / 0   (invalid readings: 0)
Residue false positives: 0   suspect re-tests: 0
Sober people wrongly sent home: 2   | drunk missed by machine: 0
Long-wait escalations  : 27   | machines used: 2
Machine breakdowns     : 0
Security incidents (entered while tablet != ALLOWED): 8
======================================================================
```

## Important notes

- The simulation is intentionally designed around concurrency and race conditions.
- The dashboard is meant to make the output easier to read and more manager-friendly.
- The code uses standard Python libraries only.

## Troubleshooting

If the simulation does not start:

```bash
python --version
```

Make sure Python 3.8 or later is installed.

If you want to read the raw command-line output without the dashboard:

```bash
python warehouse_sim.py --speed 0.2
```

## Summary

This project combines a realistic warehouse-entry simulation with a dashboard that makes the operational picture easy to understand at a glance. It is useful for demonstrations, testing queue behavior, and exploring how system stress changes outcomes.

