# Product

<!-- impeccable:product-schema 1 -->

> Inferred from the build brief (SPEC.md part 3, section "3. Frontend") by an agent with no way to ask the
> user. Every fact below comes from that brief; items marked *(inferred)* are interpretation, not confirmation.

## Name

My2cents.ai Lambda MicroVM Playground (the sidebar lockup reads "my2cents.ai" over "Lambda MicroVM Playground").

## Platform

web

## Users

A single signed-in developer (the blog author, and readers who deploy the companion code) who runs a few
AWS Lambda MicroVM sandboxes and works inside them through a browser terminal, usually running Claude Code.
They are technical, AWS-literate, and use the playground on a laptop or desktop monitor (1280-1920 px) for
long working sessions. *(inferred: sessions of tens of minutes to hours, alongside an editor and docs.)*

## Product Purpose

Manage several Lambda MicroVM sandboxes (create, suspend, resume, terminate), open a terminal in each with
Claude Code ready, and watch what the sandboxes do on the network through iron-proxy: requests, credential
swaps, DNS queries, token usage, and the VM's vitals and lifecycle timers. Success: the user can work in a
sandbox and, at a glance, see what it is talking to, which secrets were injected, and how long it has left.

## Positioning

The sandboxes never hold real credentials: iron-proxy swaps placeholders for real secrets on the way out, and
the UI shows each swap, lets the user revoke a credential for every sandbox at once, and shows MicroVM
suspend/resume behaviour (idle timers, 8 h cap, auto-resume) that a plain cloud console hides.

## Operating Context

- Terminal-first: the xterm.js terminal is where the work happens (tmux session `main`, Claude Code TUI).
- Panels are monitoring, read at a glance while the terminal has focus.
- MicroVMs suspend when idle; open WebSockets die on suspend/resume and must reconnect.
- Polling the in-VM agent keeps the VM awake, so live stats only flow while a terminal is attached.
- The proxy feed and DNS log cannot be attributed to one sandbox (all traffic arrives from the NLB IPs).

## Capabilities and Constraints

- REST + WebSocket API defined in SPEC.md section 2; Cognito ID token auth (PKCE via managed login).
- Account quota: 8 GB MicroVM memory, 2 GB per sandbox, suspended ones count.
- Max sandbox lifetime 8 h (counts suspended time); idle suspend after N minutes (default 15).
- Static SPA served from S3 via CloudFront; `/config.json` written at deploy time.
- Laptop/desktop first; degrade sensibly to tablet; phone only needs to not break.

## Brand Commitments

- Dark-first, information-dense but calm. No marketing fluff in copy. *(from the brief)*
- Part of the my2cents.ai blog's companion code; no separate brand assets exist.

## Evidence on Hand

No real data in the repo. Mock mode (`VITE_MOCK=1`) generates synthetic sandboxes, proxy events, DNS
queries, usage and vitals; those values are illustrative only.

## Product Principles

1. The terminal is the work; everything else is peripheral vision.
2. Say plainly what each number covers and what an action affects (all sandboxes vs this one).
3. Never cost the user money or uptime by accident: no polling that keeps a VM awake unless attached.
4. Connection state is always visible and recoverable in one action.

## Accessibility & Inclusion

No product-specific requirement stated. Target WCAG 2.2 AA contrast and full keyboard operation. *(inferred)*
