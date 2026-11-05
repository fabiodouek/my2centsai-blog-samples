---
name: My2cents.ai Lambda MicroVM Playground
description: A night desk for sandboxes; the desk keeps the real keys and the clerk sees every one that leaves.
colors:
  n0: "#0e0c09"
  n1: "#14110e"
  n2: "#1a1713"
  n3: "#211d1a"
  n4: "#2d2925"
  n5: "#3f3b36"
  n6: "#5f5a54"
  n7: "#969189"
  n8: "#b8b3ac"
  n9: "#e4e1da"
  n10: "#f7f5f1"
  brass: "#e1b761"
  brass-hi: "#ecc77a"
  brass-ink: "#1c1407"
  brass-wash: "rgb(225 183 97 / 0.1)"
  brass-wash-2: "rgb(225 183 97 / 0.18)"
  brass-line: "rgb(225 183 97 / 0.42)"
  run: "#69c898"
  run-wash: "rgb(105 200 152 / 0.12)"
  sleep: "#92a2db"
  sleep-wash: "rgb(146 162 219 / 0.12)"
  deny: "#e66f64"
  deny-hi: "#ee857b"
  deny-ink: "#1d0a07"
  deny-wash: "rgb(230 111 100 / 0.12)"
  deny-line: "rgb(230 111 100 / 0.4)"
  series-1: "#3987e5"
  series-2: "#d95926"
  series-3: "#199e70"
  series-4: "#c98500"
  series-other: "#5f5a54"
typography:
  title: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif", fontSize: "0.875rem", fontWeight: 600, lineHeight: 1.45 }
  body: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif", fontSize: "0.8125rem", fontWeight: 400, lineHeight: 1.45 }
  label: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif", fontSize: "0.75rem", fontWeight: 500, lineHeight: 1.45 }
  micro: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif", fontSize: "0.6875rem", fontWeight: 600, lineHeight: 1 }
  figure: { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif", fontSize: "1.0625rem", fontWeight: 600, lineHeight: 1.2, fontFeature: "tnum" }
  mono: { fontFamily: "ui-monospace, 'SF Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace", fontSize: "11.5px", fontWeight: 400, lineHeight: 1.45 }
rounded:
  hairline: "2px"
  sm: "4px"
  md: "6px"
  lg: "10px"
spacing:
  xs: "4px"
  sm: "8px"
  gutter: "14px"
  row: "28px"
  bar: "40px"
components:
  button-primary: { backgroundColor: "{colors.brass}", textColor: "{colors.brass-ink}", rounded: "{rounded.md}", padding: "0 10px", height: "28px" }
  button-primary-hover: { backgroundColor: "{colors.brass-hi}" }
  button-primary-disabled: { backgroundColor: "{colors.n3}", textColor: "{colors.n7}" }
  button-secondary: { backgroundColor: "{colors.n3}", textColor: "{colors.n9}", rounded: "{rounded.md}", padding: "0 10px", height: "28px" }
  button-secondary-hover: { backgroundColor: "{colors.n4}", textColor: "{colors.n10}" }
  button-ghost: { textColor: "{colors.n8}", rounded: "{rounded.md}", padding: "0 10px", height: "28px" }
  button-ghost-hover: { backgroundColor: "{colors.n3}", textColor: "{colors.n10}" }
  button-danger: { textColor: "{colors.deny}", rounded: "{rounded.md}", padding: "0 10px", height: "28px" }
  button-danger-hover: { backgroundColor: "{colors.deny-wash}" }
  button-danger-solid: { backgroundColor: "{colors.deny}", textColor: "{colors.deny-ink}", rounded: "{rounded.md}", padding: "0 10px", height: "28px" }
  button-danger-solid-hover: { backgroundColor: "{colors.deny-hi}" }
  state-pill-running: { backgroundColor: "{colors.run-wash}", textColor: "{colors.run}", typography: "{typography.micro}", padding: "0 8px 0 7px", height: "20px" }
  state-pill-suspended: { backgroundColor: "{colors.sleep-wash}", textColor: "{colors.sleep}", typography: "{typography.micro}", padding: "0 8px 0 7px", height: "20px" }
  state-pill-denied: { backgroundColor: "{colors.deny-wash}", textColor: "{colors.deny}", typography: "{typography.micro}", padding: "0 8px 0 7px", height: "20px" }
  state-pill-terminated: { backgroundColor: "{colors.n3}", textColor: "{colors.n7}", typography: "{typography.micro}", padding: "0 8px 0 7px", height: "20px" }
  fob-board: { backgroundColor: "{colors.brass-wash-2}", textColor: "{colors.brass-hi}", padding: "0 10px 0 6px", height: "24px" }
  fob-feed: { textColor: "{colors.brass-hi}", padding: "0 6px 0 3px", height: "18px" }
  fob-empty: { textColor: "{colors.n7}", padding: "0 10px 0 6px", height: "24px" }
  input: { backgroundColor: "{colors.n0}", textColor: "{colors.n10}", rounded: "{rounded.md}", padding: "0 9px", height: "30px" }
  segmented-item-active: { backgroundColor: "{colors.n4}", textColor: "{colors.n10}", rounded: "{rounded.sm}", padding: "0 8px", height: "22px" }
  sandbox-row-selected: { backgroundColor: "{colors.n3}", rounded: "{rounded.md}", padding: "8px 10px 9px" }
  dialog: { backgroundColor: "{colors.n2}", textColor: "{colors.n9}", rounded: "{rounded.lg}", padding: "18px 20px 16px", width: "440px" }
---

# Design System: My2cents.ai Lambda MicroVM Playground

## Overview

**Creative North Star: "The Night Desk"**

The playground is a hotel night desk after hours. The desk (iron-proxy) holds the real keys, the rooms (sandboxes) hold only placeholders, and the clerk can see every key that leaves the board. The lobby is a warm charcoal ramp, lit by one brass lamp; everything else is quiet until a state changes.

It is dense but calm: a 28px row module, 11 to 14px type, hairline dividers, and no cards for metrics. The terminal owns the centre and is the darkest surface; sidebar and panel are one step lighter and serve as peripheral vision. Colour carries meaning only. It refuses the cool-slate cloud console with a neon cyan accent and a card grid of metrics.

**Key Characteristics:**
- One warm neutral ramp (n0 to n10), dark-first, no light theme.
- One accent, brass, rationed to action, focus, selection and keys.
- Four state hues and nothing else: verdigris, night indigo, oxide red, ramp grey.
- Credentials drawn as brass key tags with a punched hole.
- Flat surfaces; shadows only on things that float.

## Colors

A low-chroma warm charcoal lobby, one brass lamp, and state hues that only speak when something is true.

### Primary
- **Desk Brass** (brass): primary button, focus ring, caret, active tab underline, selected sandbox outline (brass-line), splitter hover, the newer-events pill, key-usage bars, the brand mark and every key fob. **Brass Highlight** (brass-hi) is its hover and the fob label; **Brass Ink** (brass-ink) is text on brass. Washes tint selection (brass-wash-2) and the input focus halo (brass-wash).

### Secondary (state hues)
- **Verdigris** (run): running, starting and resuming; live feed dot; success toasts.
- **Night Indigo** (sleep): suspended and suspending; the suspended terminal overlay.
- **Oxide Red** (deny): denied requests, terminating, lifetime under 30 minutes, quota full, the off-allowlist DNS flag, NXDOMAIN, form errors, destructive actions. Denied feed rows and NXDOMAIN DNS rows take a faint tint of it (`rgb(230 111 100 / 0.055)`, 0.1 on hover).
- **Ramp Grey** (n6 / n7): terminated and unknown. Terminated is grey, never red.

### Tertiary (data series)
- **Series 1 to 4 and Other** (series-1..4, series-other): the categorical palette for per-host columns and line charts, validated on n2 (#1a1713) with the dataviz validator in dark mode. Single-series line charts use series-1.

### Neutral
- **Terminal Night** (n0): terminal background, inputs, segmented track, fob hole.
- **Lobby** (n1): app, sidebar, panel and toolbar surface.
- **Raised** (n2): dialogs, overlay cards, the new-sandbox form, expanded feed detail, row hover, row dividers inside lists.
- **Hover / Selected** (n3, n4): selected row, secondary button, pressed segment; n4 is also the standard hairline (line) and life-bar track.
- **Strong Line** (n5): input borders, dialog and toast borders, table totals.
- **Text** (n6 faint, n7 muted, n8 secondary, n9 primary, n10 high): labels and timestamps sit at n7; names, titles and figures at n10.

### Named Rules
**The Brass Budget Rule.** Brass marks the primary action, focus, selection and keys. It is never decoration, never a chart series, never a state.

**The Meaning-Only Rule.** A state hue appears only when that state is true. There is no warning orange: anything that needs attention (time running low, quota full, off-allowlist) is oxide red.

**The One Ramp Rule.** Every neutral comes from n0 to n10. Do not introduce a grey that is not on the ramp.

**The Series Are Not States Rule.** series-1..4 live inside charts only; state is never encoded with them, and chart lines never borrow state hues.

## Typography

**Body Font:** system-ui (with -apple-system, Segoe UI, Roboto, Helvetica Neue, Arial)
**Mono Font:** ui-monospace (with SF Mono, Menlo, Consolas, Liberation Mono)

**Character:** The platform's own UI sans, small and tight, so the playground reads as a tool next to the terminal. Mono is reserved for what a machine would type.

### Hierarchy
- **Title** (600, 14px): sandbox name in the toolbar, overlay titles. Dialog titles step to 15px and the sign-in title to 17px; nothing is larger.
- **Body** (400, 13px, 1.45): default text, pane titles at 600.
- **Label** (500, 12px): field labels, section titles (600, n8), chart titles, notes, tabs and buttons at 12.5px.
- **Micro** (600 or 400, 11px): state pills, timestamps, table headers, legends, fact labels.
- **Figure** (600, 17px, 1.2, tabular): key counts and usage totals.
- **Mono** (400, 11.5px; 10.5px for methods and legend keys; 13px in the terminal): hosts, paths, ids, placeholders, DNS types and rcodes.

### Named Rules
**The Mono Means Machine Rule.** Mono only for the terminal, hosts, paths, ids, HTTP methods and DNS codes. Labels, numbers and prose stay in the UI sans.

**The Tabular Figures Rule.** Every changing number (timers, counts, bytes, quota, axis labels) uses tabular numerals.

**The Sentence Case Rule.** Labels are sentence case at their natural weight; there is no uppercase or letter-spaced label style.

## Layout

Three columns: sidebar 252px, terminal flexible, right panel 400px by default (resizable 320 to 680px through a 1px splitter that turns brass on hover). Every bar (sidebar head, toolbar, tab strip) is 40px; every list row (feed, DNS, timeline, buttons) sits on the 28px module. Panes use a 14px gutter, 12 to 14px vertical padding, and sections separated by a hairline with 22px above and 18px below.

Below 1100px the panel becomes a fixed drawer on the right (min of its width and 92vw); below 760px the sidebar also becomes a drawer (min 300px, 88vw) behind a scrim. The toolbar is a container: buttons collapse to 26px icon-only in stages at 900, 720 and 520px, the primary last.

**The 28px Row Rule.** One row module across sidebar, feed, DNS, tables and timeline; do not invent a second row height.

**The Fixed Scale Rule.** Every life bar uses one 8 h scale, so sandboxes compare at a glance; time beyond a sandbox's own cap is drawn as track, not stretched.

## Elevation & Depth

Flat by default. Depth comes from the ramp: terminal (n0) below the lobby (n1), raised surfaces at n2, hover and selection at n3 and n4, divided by 1px hairlines. Only things that float above the work cast a shadow.

### Shadow Vocabulary
- **Overlay** (`box-shadow: 0 12px 32px -8px rgb(0 0 0 / 0.6), 0 2px 6px rgb(0 0 0 / 0.4)`): dialogs, toasts, chart tooltips, the terminal connection card, the newer-events pill, drawers in narrow layouts.

### Named Rules
**The Overlays-Only Rule.** Rows, panes, panels and form surfaces never carry a shadow; selection is an inset brass hairline, not a lift.

## Shapes

Gently rounded and small: 6px on buttons, inputs, rows, notes and tooltips; 4px on badges and segment items; 10px on dialogs and overlay cards; 2px on life bars, quota cells and key bars. Pills (state, feed status, newer-events) are fully rounded to half their height. Borders are 1px hairlines; dashed lines mean absent (empty fob hook, chart waiting, mock badge).

**The Key Tag Silhouette.** The fob is round on the left and squared on the right (9px 4px 4px 9px; 12px 5px 5px 12px at board size) with a punched hole near the round end. The brand mark is the same tag stood upright. Keep this silhouette for credentials only.

## Components

### Buttons
- **Shape:** gently rounded (6px), 28px tall (26px small), 12.5px at 500.
- **Primary:** brass with brass ink at 600; hover lifts to brass-hi. Disabled drops to the n3 ramp with muted text at full opacity, so it never reads as faded brass.
- **Secondary:** n3 fill with an n5 hairline; hover n4. **Ghost:** n8 text, n3 on hover.
- **Danger:** oxide text, oxide wash on hover; the solid form (destructive confirm only) is oxide with deny-ink, hover deny-hi.
- **States:** 160ms colour transitions on `cubic-bezier(0.16, 1, 0.3, 1)`; press nudges 0.5px down; focus is a 2px brass outline at 1px offset; busy shows a spinner in place of the icon.

### State pill and dot
A 7px dot in the state hue plus one word, on a 12% wash of the same hue. Transitional states (starting, resuming, suspending, terminating) pulse the dot in the target state's hue. Colour plus one word, never two labels.

### Key fob (signature)
- **Board (Credentials):** filled, brass-wash-2 with a brass-line border, brass-hi label, 24px.
- **Feed:** the same tag as a transparent outline, 18px, 10.5px label, on every proxy row where that key was swapped in.
- **Revoked:** an empty hook: dashed n6 border, muted label, hollow hole.

### Inputs / Fields
- **Style:** n0 well, n5 hairline, 6px, 30px tall, n10 text, brass caret.
- **Focus:** border turns brass with a 3px brass-wash halo.
- **Error:** oxide border with an oxide message below.

### Segmented filter
n0 track with a hairline and 2px inset; items 22px, muted; active item n4 with n10 text and a tabular count.

### Navigation
Sandbox rows: 6px rows with name, state word, a 3px life bar and a micro meta line; hover n2, selected n3 with an inset brass-line ring, ended rows at 60% opacity. Panel tabs: muted text, active n10 with a 2px brass underline sitting on the strip's hairline.

### Overlays, dialogs, toasts
Terminal connection card: n2, 10px, overlay shadow, 34px round icon. Connecting, reconnecting, detached and shell-exited cards are neutral (n4 icon); suspended uses the indigo wash; connection errors use the oxide wash. The terminal dims to 28% beneath it. Dialogs: 440px, n2, 10px, focus lands on Cancel. Toasts: n3, 8px, a 7px leading dot (oxide for errors with an oxide border, verdigris for success).

### Quota meter
One cell per GB of the account's MicroVM memory quota (eight here; above 16 GB a cell stands for several GB), so a sandbox takes as many cells as its size. 8px tall, a 1px gap inside a sandbox and 3px between sandboxes: verdigris for running, indigo for suspended, a diagonal n6/n4 hatch for memory held elsewhere, an n5 inset outline for free.

## Do's and Don'ts

### Do:
- **Do** keep brass to the primary action, focus, selection and keys (The Brass Budget Rule).
- **Do** use oxide red for anything that needs attention; there is no warning orange.
- **Do** put every list on the 28px row and every bar at 40px.
- **Do** draw a credential as the key tag: filled on the Credentials board, outline in the proxy feed, dashed empty hook when revoked.
- **Do** use tabular numerals for every live number and mono only for machine strings.
- **Do** use the validated series-1..4 palette, in order, for chart categories, with series-other for the remainder.

### Don't:
- **Don't** use the cool-slate cloud console look: no cool greys, no neon cyan accent, no card grid of metrics.
- **Don't** add a grey outside the n0 to n10 ramp, or a light theme.
- **Don't** paint a terminated sandbox's state pill, dot or life bar red; terminated is ramp grey.
- **Don't** put shadows on rows, panes or panels; shadows belong to overlays only.
- **Don't** label anything twice: a state is its hue plus one word.
- **Don't** overlay the terminal with anything except its own connection state.
