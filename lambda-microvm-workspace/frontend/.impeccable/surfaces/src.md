---
version: 1
slug: "src"
primary_target: "src"
related_targets: []
---

# Workspace console (frontend/src)

Scope: the whole signed-in SPA (sidebar, terminal, right panel). Mode: Operate. Built code-led.
Audience/job: one developer working inside Lambda MicroVM sandboxes through a browser terminal, glancing at
proxy traffic, credential swaps, token usage, vitals and DNS. Constraints: SPEC.md section 3; brief pins
dark-first, dense but calm, serious developer tool, no marketing copy.

Decision note: run unattended (subagent with no question channel). Grounded list, by resonance:
1 railway track-occupancy diagram, 2 airport departures board, 3 hotel night desk key board,
4 customs stamp ledger, 5 ATC flight progress strips, 6 SBB station clock, 7 library due-date card.
Assigned: 3. Challengers all declined (marketing monochrome, botanical folio, Crouwel grid, vertical feed,
Saville catalog, exposure record); their disciplines are kept as raises below.

## Direction contract

THESIS: The workspace is a hotel's night desk: the desk (iron-proxy) keeps the real keys, rooms (sandboxes)
only hold placeholders, and the clerk sees every key that leaves the board. Refuses the cool-slate cloud
console with a neon cyan accent and a card grid of metrics.

OWN-WORLD: an eleven-step warm charcoal ramp (night lobby, chroma <= 0.01, hue ~70) is the only neutral set;
brass is the single accent and only marks the primary action, focus, selection and key fobs. State hues:
verdigris = running, night indigo = suspended, oxide red = denied/terminating, ramp grey = terminated;
transitions pulse in the target state's hue. System UI sans, tabular numerals; mono only for terminal,
hosts, paths, ids. Hairlines, 6 px radii, shadows only on overlays.

STORY: pick a sandbox, work in its terminal, glance right to see what left the building and which keys were
swapped in, revoke a key for every sandbox when needed, and always know how long a sandbox has left.

FIRST VIEWPORT: three columns at 1440: 248 px sidebar (sandbox rows with state, fixed-scale 8 h life bar,
idle countdown; New sandbox at top; quota as four 2 GB cells at the bottom), terminal centre with a 40 px
toolbar (name, state, timers; Start Claude Code primary in brass; Suspend, Resume, Detach, Terminate),
right panel 400 px (resizable) with tabs Proxy, Keys, Claude Code, Vitals, DNS; Proxy open by default.

FORM: hotel night-desk key board, candidate 3 of 7 (assigned), seed:9dbeb706fb9e9b83. Signature move: each
credential is a brass key fob (tag with a punched hole); the same fob appears on the Keys board, on every
proxy row where that key was swapped in, and as an empty hook outline when revoked.
Raised by botanical folio: one fixed 8 h scale for every life bar so sandboxes compare at a glance.
Raised by Crouwel grid: one 28 px row module across sidebar, feed, DNS and tables.
Raised by monochrome marketing: brass budget, never decoration.
Raised by Saville catalog: nothing labelled twice; colour plus one word, never two labels.
Raised by exposure record: the ramp is the only neutral token set.
Raised by vertical feed: the terminal owns the centre; only its own connection state overlays it.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
