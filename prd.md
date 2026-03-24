# BMAD Autopilot PRD

## Product Intent

BMAD Autopilot is a standalone local product that keeps BMAD-style coding work moving while the user is away.
Its job is to turn a repo, a workflow definition, and a quota-capable Codex account pool into unattended progress.
The product is not an editor extension, not a chat wrapper, and not a feature module inside another product.
It is a self-contained orchestrator with its own config, state, logs, retry policy, and recovery behavior.

The core promise is progress-first execution.
The product prefers retry, reroute, account rotation, and timed backoff over stopping.
It only becomes terminal when no usable account remains, or when the workspace is unrecoverably unsafe to continue.

## Problem Statement

AI coding tools are useful only when they keep working after the first failure.
In practice, unattended coding breaks for reasons that are usually temporary:

- a model returns malformed structured output
- a review pass rejects the current work and wants another dev iteration
- a workspace becomes dirty or stale
- an account runs low on quota
- a session needs to be resumed instead of restarted

Most assistants treat these as stop conditions.
BMAD Autopilot treats them as workflow events.

The user need is simple:

1. Start coding work before leaving the machine.
2. Let the system continue through dev, QA, and review without babysitting.
3. Retry transient failures automatically.
4. Rotate accounts when quota pressure appears.
5. Preserve enough state and logs to recover a run after interruption.

## Product Boundary

BMAD Autopilot is a standalone product with the following boundary:

- It owns orchestration.
- It owns state persistence.
- It owns account switching.
- It owns retry and backoff.
- It owns artifact placement and run logs.

It does not depend on another editor product, another coding assistant UI, or a host application shell.
It may integrate with git, Codex CLI, GitHub, or Cockpit-style account storage, but those are adapters, not the product.

The product can be launched from a terminal, cron, systemd, launchd, or CI.
The control surface is the launcher and config files, not an embedded UI.

## Product Goals

### Goal 1: Unattended progress

The product advances BMAD work while the user is away.
It should keep writing code, re-running checks, and re-invoking review passes whenever the input and workspace still admit progress.

### Goal 2: Retry-first behavior

The product treats malformed output, transient validation failures, and review rejection as recoverable.
It retries the same task with the same workspace context instead of discarding the session.

### Goal 3: Quota-aware continuity

The product switches to a healthier account when the current account is close to quota exhaustion or is otherwise below threshold.
It should only stop for quota reasons when all usable accounts are exhausted.

### Goal 4: Workspace fidelity

All state, artifacts, logs, and review history are bound to the active workspace.
The product must not reuse stale artifacts from another workspace as if they belonged to the current run.

### Goal 5: Standalone operation

The product must be valuable on its own.
It should not require a larger platform to be useful, and it should not rely on the user staying inside an editor or web UI.

## Non-Goals

BMAD Autopilot is not trying to be:

- a general-purpose chat assistant
- a code editor
- a browser plugin
- a benchmark generator
- a CAD authoring environment
- a CI platform
- a GitHub workflow replacement

It also is not trying to pretend that unsafe or corrupted states are safe.
The product should prefer progress, but it should not fabricate successful outcomes when the workspace or quota state is actually unrecoverable.

## Target Users

### Solo operator

An engineer who wants to start a task, leave the machine, and return to progress instead of prompts.

### Small team operator

A lead or builder who wants a local automation loop that can continue through overnight coding, review, and test cycles.

### Automation maintainer

A user who cares about logs, reproducibility, account rotation, and deterministic recovery more than polished UI chrome.

## Core Experience

The default experience is simple:

1. The user points the product at a git workspace.
2. The product finds the next actionable BMAD story or epic.
3. It runs the correct workflow phase.
4. It validates structured output.
5. It retries or reroutes when the output is not good enough.
6. It continues until work is complete, the queue is exhausted, or every account is out of quota.

The product should always feel like an execution engine, not a conversational agent.

## Functional Requirements

### Workspace handling

- The product resolves the active repository root before starting work.
- The product stores state and artifacts inside the workspace, not in a global shared scratch area.
- The product supports dirty-worktree confirmation before running in a modified repository.
- The product resumes the previous run by default.
- The product supports a fresh-start mode for explicit resets.

### Workflow routing

- The product routes work to the correct BMAD phase based on state and validation result.
- The product moves from dev to QA to review to completion when each gate passes.
- The product reroutes invalid dev output back to development.
- The product reroutes QA rejection back to development.
- The product reroutes invalid code-review output back to development.
- The product does not convert a rejection into a success-like terminal state.

### Structured output validation

- The product requires machine-readable output contracts for agent phases.
- The product validates required fields before state transitions.
- The product rejects malformed output instead of inferring intent from prose.
- The product keeps review and dev decisions tied to the current workspace snapshot.
- The product preserves exact fingerprints and scope checks for review output.

### Session continuity

- The product captures a resumable Codex session identifier.
- The product resumes the same Codex conversation when validation fails and a retry is appropriate.
- The product keeps the original task context stable across retries.
- The product increments retry attempts without losing the original story or epic identity.

### Quota and account management

- The product reads available account state from the configured account store.
- The product switches to a healthier account before launching a new Codex session when thresholds are crossed.
- The product may switch accounts at startup and before each session.
- The product only enters a terminal quota stop when no usable account remains.
- The product records account-switch decisions in logs for traceability.

### Retry and recovery

- The product treats transient `stories_blocked` dev responses as retryable, not terminal.
- The product keeps the story in `in-progress` during a transient block.
- The product reroutes transient `stories_blocked` development responses immediately and only waits when Codex quota is exhausted and no healthier account is available.
- The quota-wait interval is configurable for testing and operations.

### Observability

- The product logs phase transitions, validation failures, account switches, and retry decisions.
- The product persists review artifacts and code-review provenance under the active workspace.
- The product keeps enough history to explain why a run retried or switched accounts.
- The product records resumable session IDs when the Codex CLI emits them.

### Recovery

- The product can be restarted without losing the current story or epic context.
- The product can restore its state from on-disk metadata.
- The product should not need the user to reconstruct the run manually after a transient failure.

## Product Rules

The following rules define the behavior that makes the product feel "infallible" without becoming dishonest:

1. Prefer progress over stopping whenever the next safe step is known.
2. Retry transient failures before asking for human intervention.
3. Rotate accounts before declaring quota exhaustion.
4. Keep a failed story in progress when the failure is meant to be retried.
5. Use terminal blocked state only when every usable account is exhausted or the workspace cannot be continued safely.
6. Never infer success from a clean-sounding response when structured validation fails.
7. Never hide the reason for a retry, reroute, or backoff.

## Packaging And Distribution

The product should ship as:

- a CLI launcher
- a background-capable state machine
- a config file driven runtime
- a log-oriented local service

It should be easy to place into a repository and run without wiring up a host application.
The product may use BMAD workflow templates as inputs, but the product boundary remains the autopilot runner itself.

## Success Criteria

The product succeeds when it can do the following reliably:

- start from a workspace and continue unattended
- retry malformed dev, QA, or review outputs without collapsing the run
- switch accounts when quota pressure appears
- back off on transient blocked dev passes instead of stopping
- resume a previous run after interruption
- keep artifacts and review output tied to the correct workspace

Operational success is measured by:

- fewer manual restarts
- fewer false terminal blocks
- better overnight completion rate
- accurate account rotation
- clean recovery after session interruption
- no cross-workspace artifact leakage

## Release Shape

### MVP

- story-first workflow orchestration
- dirty-worktree confirmation
- resumable Codex sessions
- retry and reroute on validation failure
- quota-aware account switching
- workspace-scoped review artifacts
- transient dev-block reroute

### V1

- legacy epic/PR flow compatibility
- richer operational metrics
- more granular account health selection
- operator controls for quota-wait and retry tuning

### V2

- multi-workspace fleet operation
- optional remote monitoring
- scheduled overnight policies
- team-level reporting and history summaries

<!-- Future work: define a formal service-level objective for unattended overnight completion rate once the product has enough telemetry. -->
