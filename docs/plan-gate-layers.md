# Plan-gate: layers (walls, liq, stops, tp, CVD)

Status: Q1–Q3 decided. **PR7 merged** 2026-09-15 (`83ff216`). **PR8 merged** 2026-09-15 (`2916d0a`). **PR9 on cc** (not gb), spec `~/Dev/Hermes/handoffs/liq-tape-pr9-liq-map.md` — it changes three PR9 details below: one `liqmap --coins` call instead of one per coin, liqmap runs in the background, and the leaderboard cache lives beside the client.
Source: grok-build, 2026-09-14. Mock sent to Klud TG.

This is the SSOT for gb / cc / agy. Do not fork a second plan in a handoff.

---

## Goal

Add a read-only **price × time layer panel** and the supporting data so the board can show, as structure not as cues: resting **walls** (size encoded as thickness), **real Hyperliquid liquidation prices** with wallet concentration, **stop** and **take-profit** orders from the same wallet universe, **swept vs standing** cluster state, **2% / 5% distance-to-liq buckets**, a **right-side liq-notional profile**, **CVD** next to the existing sampled tape, and an honest **methodology** line (coverage, age, HL only). Same doctrine as today: loopback, stdlib, no keys, no orders, no signals, CSS tokens only, sampler is the only writer. Acceptance: each PR below is mergeable on its own, CI walls stay green, a missing new file is an empty/veil state not a fabricated number, and GitHub PRs are recorded in `docs/NUMBERING.md` when they land.

---

## Blocking questions (reply “yes to all” or change a default)

1. **Liq universe = leaderboard top 200, polled every 120s, via a new client subcommand `liqmap` — decided yes (2026-09-15).** Real `liquidationPx` is already on `clearinghouseState`. One client call fan-outs HTTP internally; sampler writes `data/liq_<COIN>.jsonl` on a **slower clock than the 12s OI tick**. Methodology must say this is the largest accounts, not the whole book (see pulse note under Assumptions).

2. **Client change stays outside this repo — decided yes (2026-09-14).** Same as PR6 `trades`. Path: `~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py`. Web access is a Cloudflare tunnel to this Studio (`gb.` / `wt.` / `bus.` pattern), so the helper path still exists. Do not vendor a network stack into `server.py` / `sampler.py`. Do not bind `0.0.0.0`.

3. **New panel ⑧ is time × price — decided yes (2026-09-15).** Wilson likes the mock: **one screen, no extra scroll.** ⑧ is the main canvas (layers + cluster card + right-side profile). Existing quadrant, L2, tape, CVD sit around it like the mock, not stacked as another full-width block under ⑦. Volume profile / funding / hand levels may sit below the fold or behind a chip — they must not push ⑧ off-screen. Do not draw liq/walls on the OI quadrant or on panel ⑦.

---

## Assumptions

1. **Pulse vs top 200.** Leaderboard top 200 is account-value / PnL, not “closest to liq.” It will show whale magnets and named-wallet fuel (the James Wynn shape). It will **miss** crowded mid-size leverage that is not on the leaderboard — that is often the cascade pulse. v1 is honest about that: coverage line is `N of 200 largest`, 2%/5% buckets and wallet-count are the pulse *inside that set*. Do not imply “the market.” Later (not this sequence): raise N toward Snappy’s ~4k, or add a second sort by distance-to-liq. 200 @ 120s is the rate-limit fit.

2. **Data.** Walls come from the existing L2 payload (`px`, `sz`, `orders`) already fetched every 2s, 15 levels/side. **PR7 review (2026-09-15):** 4× median **or $1M, whichever is larger** never fired on the live 15-tick book (BTC max ~$630k vs $1M floor; ETH 4× median still above the fattest level). Replacement: a level is a wall if its notional is **≥ 1.5× the median of that side**, or **≥ 20% of that side’s visible notional**. No global dollar floor. `wall_threshold` stays in the payload (report both the 1.5× median value and the 20% share cutoff). Fewer than 3 levels on a side → no walls. Do not raise `L2_LEVELS` in the fix. CVD is the cumulative signed sampled tape from `data/trades_<COIN>.jsonl` (side `B` = +, `A` = −, notional `px*sz`). That file is last-~10 prints per 12s poll, not a full tape; the panel must say so. Mark path for panel ⑧ is the sampler’s `oi_<COIN>.jsonl` mark (12s). Liq rows are `{address, coin, szi, liquidation_px, position_value}` clustered by price bucket. Stops/TPs are trigger / `frontendOpenOrders` from the **same** address set, not the whole book.

3. **Failure.** A failed `liqmap` poll never skips the OI write and cannot stretch the 12s cadence past one timeout (copy the trades-after-OI pattern). Missing `liq_*.jsonl` → 200 with empty clusters + `note`, not 404 (sampler may be older than the board). Client timeout / non-JSON → existing `ClientError` shape. Partial wallet fetch (180/200) → serve what arrived, set `coverage` and `capped: true`.

4. **Boundaries.** Public API additions: `/api/walls/<COIN>` (or tagged fields on `/api/l2`), `/api/cvd/<COIN>`, `/api/liq/<COIN>`, later `/api/triggers/<COIN>`. Existing `/api/oi`, `/api/l2`, `/api/prints`, `/api/profile`, `/api/levels` keep their contracts. Kill switches on the sampler: `--no-liq`, `--no-triggers`. No SSE; keep HTTP poll. Bind stays `127.0.0.1`. **Web (decided 2026-09-14):** same `studio-gb` Cloudflare tunnel as `gb.wilsco.au` → `:7681`, `wt.wilsco.au` → `:8766`, `bus.wilsco.au` → `:8787`. `liq.wilsco.au` is live (2026-09-15): tunnel → auth proxy `:8792` → board `:8791`. Same basic-auth as `wt.` (`wilsco` + `~/.cloudflared/ttyd-pass`). Not a VPS, not Vercel, not in PR7–12.

5. **State.** Sampler is the only writer. Server still has no `open(..., "w")`. Cluster “swept” is computed by the sampler from the previous liq snapshot + current mark, then appended; the page does not invent history. Idempotent jsonl append, dedupe on `(ts_bucket, coin)` or a `seq` field.

6. **Environment.** Python 3.11, stdlib only, official client subprocess **on this Studio**. CI is `.github/workflows/verify.yml`. Hardcoded `#hex` in SVG `fill`/`stroke` fails CI — use CSS tokens. `support|resistance` in `index.html` / `README.md` fails CI. `buy|sell|long|short|signal|alert|support|resistance` in `levels.yaml` fails CI. Do not put `api_key` / `secret` / `password` / `token` in `.py`. Sampler must not mention the levels file. Studio off → tunnel 404; that is accepted.

7. **Scope not doing.** No CEX aggregation, no CoinGlass-style modeled heatmap, no alerts, no Telegram, no Bookmap replay, no MMT scripting, no HL node/replica, no second daemon, no npm. “Longs / shorts” as **side labels on a cluster** are data, not signal copy; keep them off `levels.yaml`. Venue is Hyperliquid only; the page says so.

8. **Testing.** PR7+: py_compile, walls, smoke curl for new routes (missing-file 200, unknown coin 404). PR9: unit the cluster function with a fixture of 5 wallets (one cluster of 2, one singleton, one other coin dropped). No live-network CI. Local verify: one `liqmap BTC --json` against the real client before claiming the API works.

---

## What is already on disk (do not rebuild)

| Piece | Where |
| --- | --- |
| OI + mark jsonl, 12s | `sampler.py` → `data/oi_<COIN>.jsonl` |
| Sampled tape, last ~10/poll | `sampler.py` → `data/trades_<COIN>.jsonl` |
| L2 15 levels/side, 2s cache | `server.py` `build_l2` → `/api/l2/<COIN>` |
| Prints + near-level tags | `/api/prints/<COIN>` |
| Volume profile 48 buckets | `/api/profile/<COIN>` panel ⑦ |
| Hand levels | `levels.yaml` panel ⑥ |
| `liquidationPx` parser | client `_normalize_positions` (not wired into liq-tape) |
| Doctrine CI | `.github/workflows/verify.yml` |

Architecture today: client subprocess → sampler writes jsonl; server reads files + live client; one `index.html`. Keep that shape. New overlay state is computed in the sampler (or derived in the server from data the sampler/client already has) and attached to the existing poll, not a parallel ticker process.

---

## Seat split

| Seat | Job |
| --- | --- |
| **agy** | Before PR9: facts-only note on HL `liquidationPx`, leaderboard (or equivalent address list), `frontendOpenOrders` / trigger fields, rate limits. Write `docs/design-liq-map.md`. No stack-fit, no code. |
| **gb** | Implement PRs 7 → 12 on branches `pr7-walls-cvd` … off `main`. One GitHub PR per project PR. |
| **cc** | Review each GitHub PR (review skill, PR mode). Empty findings: do not submit a leftover PENDING review. |

Dispatch one PR at a time. Do not parallelise PR9 with PR7 — different data, but PR8’s layer toggles are the UI PR9 hangs on.

---

## PR plan

GitHub numbers are assigned when opened; record them in `docs/NUMBERING.md`. Project sequence is PR7…PR12.

### PR7 — Walls, tape/CVD, layer chrome
**Branch:** `pr7-walls-cvd` · **Seat:** gb · **Review:** cc · **agy:** none

Data we already have. No new client command.

- `server.py`: tag L2 levels with `notional` and `wall` (boolean). Either extend `/api/l2` or add `/api/walls/<COIN>` that is a thin filter of the same cached book. Add `/api/cvd/<COIN>?lookback=15m\|1h\|4h` from the trades file; include `sampled: true` and the existing young/truncated notes. Smoke: unknown coin 404; missing trades file 200 + note.
- `index.html`: header **layer chips** (`walls`, `liq`, `stops`, `tp`, `profile`, `cvd`). Only `walls`, `profile`, `cvd` are live; the other three are present and `disabled` with a one-line “needs later PR” in the methodology note — not hidden, so the chrome does not churn. L2 bars already exist; encode wall size as bar thickness (width already maps size — make walls visibly heavier, label notional). A compact **tape** list can reuse `/api/prints` (do not duplicate the notable-prints ticks on ⑦). CVD spark under that list. Methodology strip: “HL only · sampled tape (last ~10/poll) · not a full book”.
- `README.md`: PR7 scope paragraph. No signal words.

**Rejected:** a new heatmap canvas in this PR — no time×price panel yet. **Rejected:** computing CVD in the browser from prints — server is the converter (string→float lesson from PR6).

**Done when:** walls visible on L2, CVD number matches a hand sum of the trades file over 1h, chips render, CI walls clean.

### PR8 — Panel ⑧ time × price
**Branch:** `pr8-price-layers` · **Seat:** cc (Claude Code) · **Review:** this grok-build session if needed, not gb (usage) · **Depends:** PR7 merged (chips exist)

- Compose like the mock: **one primary viewport**. ⑧ (time × mark from `oi_*.jsonl`) is the large left canvas. **Shrink the OI matrix** — it is currently a 560×520 full-width hero (`#quadrant` viewBox + `svg { max-width: 560px }`); that is why the page scrolls. Move it to a **side card ~280×260**. Readout chips stay with it, compact. L2 + prints under ⑧. Do **not** append ⑧ under ⑦ as another full-width scroller.
- Overlay: wall lines from live L2 (thickness = notional), `levels.yaml`. Layer toggles show/hide. Right-side histogram: volume profile until PR9 adds liq profile (two thin labelled histograms if both).
- CSS tokens only. No hex in SVG attributes. Veil until the OI lookback has any samples.
- Poll interval: same 5s as the quadrant; L2 walls at 2s can refresh the overlay without refetching the mark path.

**Rejected:** drawing this on the quadrant. **Rejected:** a JS charting library (no npm, no CDN — CI). **Rejected:** stacking ⑧ below ⑦.

**Done when:** first paint of the board shows ⑧ + quadrant + L2/tape without scrolling on a ~1400×900 desktop (funding / volume profile / hand levels may be below the fold); toggling `walls` hides them; dark mode uses tokens.

### PR9 — Real liq map
**Branch:** `pr9-liq-map` · **Seat:** gb · **Review:** cc · **Depends:** agy `docs/design-liq-map.md` + PR8

agy writes the design first (endpoint names, pagination, what `liquidationPx` null means, isolated vs cross, how to treat empty `szi`). gb does not guess the formula.

- **Client (outside repo):** `liqmap <coin> --top 200 --json`. One process: resolve address set, `clearinghouseState` per address with a small pool, keep rows where `coin` matches and `liquidation_px` is a finite number. Return `{coin, asof_ms, requested, fetched, positions: [...]}`. Never reads `HYPERLIQUID_USER_ADDRESS` for this command — universe is public leaderboard, not Wilson’s wallet.
- **sampler.py:** after OI (+ trades) writes, if `now - last_liq >= 120` and not `--no-liq`, run `liqmap` with a timeout that cannot delay the next OI tick (same isolation as trades). Append `data/liq_<COIN>.jsonl`. On failure, log and skip.
- **server.py:** `GET /api/liq/<COIN>`. Cluster by price bucket (width: tick or a fixed relative bin, stated in the payload). Each cluster: `px`, `side` (sign of `szi`), `notional`, `wallets` (distinct addresses), `addresses` omitted from the default payload (too wide for the page; optional `?debug=1` later, not this PR). Top-level: `within_2pct`, `within_5pct`, `largest` `{px, notional, wallets}`, `coverage` `{requested, fetched, capped}`, `age_s`, `note`. Missing file → empty + note.
- **index.html:** enable the `liq` chip. Bands on panel ⑧, cluster card (price / side / notional / wallets), distance buckets row, right-side **liq** histogram, methodology: “real `liquidationPx` from N of M largest accounts · age · HL only · not modeled OI”.

**Rejected:** CoinGlass-style leverage-tier model. **Rejected:** polling 4000 wallets at 12s. **Rejected:** putting addresses in `levels.yaml`.

**Done when:** a local `liqmap BTC --json` returns rows; the board shows clusters whose wallet counts match the fixture; coverage is visible when fetch < requested; OI jsonl cadence unchanged under a hung liqmap (force a timeout in a local test).

### PR10 — Swept vs standing
**Branch:** `pr10-swept` · **Seat:** gb · **Review:** cc · **Depends:** PR9

- Sampler keeps the previous liq snapshot in memory. A cluster is `swept` when mark has traded through its bucket since it was last standing. Append swept rows with `state: swept` and the notional **as last seen**, then drop them after a TTL (default 30 min) so the page does not accumulate a graveyard. Standing rows stay `state: standing`.
- Page: ghost bands, grey row on the cluster card, “was $X”. Not a signal that price will reverse.

**Done when:** a fixture where mark crosses a bucket flips `standing` → `swept` without rewriting history of earlier jsonl lines (append-only).

### PR11 — Stops and take-profit layers
**Branch:** `pr11-triggers` · **Seat:** gb · **Review:** cc · **Depends:** PR9 + agy note on order fields · **Kill switch:** `--no-triggers`

- Same address universe. Client subcommand `triggers <coin> --addresses-from liqmap` or extend `liqmap` to optionally include open trigger orders. Distinct types: stop vs take-profit. Payload separate (`/api/triggers/<COIN>`) so a triggers outage does not blank liq.
- Page: dashed stop / dotted tp overlays, own chips. Methodology: “triggers from the tracked universe, not the whole book”.

**Rejected:** treating resting L2 as stops.

**Done when:** chips hide independently; missing triggers file does not hide liq bands.

### PR12 — Docs close-out
**Branch:** `pr12-layers-docs` · **Seat:** gb · **Review:** cc · **Depends:** PR11 (or earlier if we stop)

README scopes, `docs/ARCHITECTURE.md` diagram, `docs/NUMBERING.md` rows, CI smoke for `/api/liq` and `/api/cvd` (unknown coin 404, missing file 200). Footer doctrine sentence updated. No new behaviour.

---

## Suggested dispatch

```
1. Wilson: Q1–Q3 decided (2026-09-15)
2. agy:  docs/design-liq-map.md          (blocks PR9, not PR7)
3. gb:   PR7  → cc review → merge
4. gb:   PR8  → cc review → merge
5. gb:   PR9  → cc review → merge
6. gb:   PR10 → cc review → merge
7. gb:   PR11 → cc review → merge
8. gb:   PR12 → cc review → merge
```

agy can run in parallel with PR7/PR8. gb must not start PR9 without the design file.

---

## Out of scope for this whole sequence

Aggregated CEX heatmaps, alerts, order placement, credentials, non-loopback bind, npm, SSE, full tape, HL node, MMT-style scripting, Coinbase round-number watch, Bybit as a second venue, Vercel/VPS rewrite, putting `liqmap` HTTP inside this repo. Tunnel hostname is ops (`~/.cloudflared/config.yml`), not a product PR.
