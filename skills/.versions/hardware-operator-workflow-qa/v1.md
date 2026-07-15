---
id: hardware-operator-workflow-qa
name: Verify Hardware and Operator Workflows
type: skill
description: Design, review, or test software that controls instruments, sensors, lab equipment, serial or socket devices, calibration state, and operator procedures. Load for hardware-in-the-loop QA, measurement software, reconnect handling, calibration lifecycle, traceability, simulated-versus-live labeling, or safety-critical operator UX.
tags: [hardware, instruments, operators, calibration, traceability]
---

# Verify Hardware and Operator Workflows

Treat the physical device, operator, software, and recorded evidence as one system.

## Map the workflow

Document:

- device identity, firmware, transport, and capabilities;
- discovery, connection, configuration, acquisition, and teardown;
- calibration type, validity, dependencies, expiry, save, recall, and invalidation;
- operator roles, decisions, prompts, and recovery;
- raw measurement, transformed result, units, limits, and audit record;
- safety interlocks and actions that require confirmation.

Clearly distinguish live hardware, replayed data, simulator, and synthetic fixtures.

## Test the lifecycle

Exercise:

- no device, wrong device, multiple devices, and hot-plug;
- slow connect, disconnect mid-operation, reconnect, and process restart;
- invalid, stale, incompatible, missing, and recalled calibration;
- partial sweep or measurement, timeout, cancellation, and retry;
- configuration change after calibration;
- unit, frequency, range, precision, and boundary errors;
- raw-data retention and deterministic recomputation;
- operator correction, repeat measurement, and comparison;
- conflicting state between device and UI;
- safe shutdown and recovery after crash.

## Verify measurement integrity

Trace every displayed or exported result back to device identity, configuration, calibration, timestamp, raw data, transformation version, and operator action. Check that charts and derived metrics use the same source state. Never present synthetic data as a live measurement.

## Review the interface

The operator should always know:

- what is connected;
- whether the system is ready;
- which calibration is active and valid;
- what is happening now;
- why an action is blocked;
- how to recover without corrupting evidence.

## Output

Return the equipment setup, lifecycle matrix, observed traces, integrity gaps, operator hazards, and paths not verified on real hardware.
