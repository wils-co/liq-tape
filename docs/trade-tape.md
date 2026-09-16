# The trade tape, in plain language

This explains where the buy/sell numbers on the panel come from, why they were
wrong before, and how much you can trust them now. No jargon required.

## What the tape is

Every time someone buys or sells BTC on Hyperliquid, that's one print on the
tape. The panel reads the tape to answer one question: **are buyers or sellers
the ones forcing the issue?**

The number that answers it is **CVD**. It adds up the dollars of everyone who
crossed the spread to buy, subtracts the dollars of everyone who crossed to
sell, and shows you the running total. Positive means buyers were the
aggressors. Negative means sellers were. That's the whole idea.

## The problem we had

The sampler used to get the tape by asking Hyperliquid, every 12 seconds,
"what just traded?" Hyperliquid always answers with **the last 10 trades** —
never more. There's no setting to ask for more; it's a hard limit on their
side, and passing `n` or `limit` is ignored.

Ten trades sounds fine until you notice it's a fixed *count*, not a fixed
*window*. When BTC is quiet, 10 trades might cover 8 seconds, so you're seeing
most of what happened. When BTC is busy, 10 trades cover half a second, and
you're blind for the other 11.5.

**So you saw the least at exactly the moment the most was going on.** Busy
moments are when the real fights between buyers and sellers happen. Those were
the moments being missed.

### How wrong it actually was

Measured on 2026-09-17: a live socket ran for 90 seconds next to the old
poller, on a **quiet** BTC tape — the conditions where the poller does its
best work.

| | trades seen | dollars seen | CVD it reported |
|---|---|---|---|
| Real tape | 271 | $647k | **−$275k** |
| Old poller | 73 | $137k | **−$30k** |

It caught about a quarter of the trades and a fifth of the dollars, and the
headline number came out **nine times too small**. The *direction* was right —
both said sellers were leaning on it — but the size meant nothing.

That's why the old caption said `sampled tape`. It was honest, but it was easy
to read the dollar figure as if it were real.

## What changed

The sampler now keeps a **live connection open** and Hyperliquid pushes every
print to it the instant it happens. Nothing is missed, because nothing is being
asked for — it just arrives.

Same 75-second test, BTC: the old path wrote 70 rows, the live path wrote 356.

The rows look identical and land in the same files, so the rest of the
dashboard didn't have to change.

## What happens when it breaks

A live connection can drop — wifi, a restart on their end, a laptop lid. When
that happens the sampler **falls back to the old 12-second polling** on the
next tick, and switches back the moment the socket reconnects. It retries on a
widening delay (1s, 2s, 4s … up to a minute) so a long outage doesn't hammer
anything.

You lose completeness while it's degraded. You don't lose the panel.

The panel tells you which one you're on. Check `tape` on `/api/cvd/<COIN>`:

- `websocket` — full tape, the dollar figure is real
- `rest` — degraded to the old sampling, read direction only
- `unknown` — sampler is older than this feature

## How to read CVD now

On the `websocket` path, the dollar number finally means what it says. You can
compare it against volume or open interest and the comparison holds.

Two things that have *not* changed, and still trip people up:

**The series restarts at zero for every window.** "CVD 1h" and "CVD 4h" are not
two readings from the same line — they're two separate lines with different
starting points. You can't subtract one from the other to get "the last three
hours."

**One big print can be the whole number.** In a 4h window on 2026-09-17, a
single $3.3M sell was 27% of the entire net, and the top 25 trades were 56% of
it. A big negative CVD can mean steady, broad selling, or it can mean three
large sellers and an otherwise balanced tape. Those are different situations.
Check the notable prints list before you decide which one you're looking at.

**What it's still best at:** disagreeing with price. Price grinding up while
CVD runs down means sellers are hitting a bid that isn't breaking. That read
worked even on the old sampled tape, and it works better now.

## Turning it off

```bash
python3 sampler.py --no-ws     # old REST-only behaviour
```

The stream needs the `websocket-client` package. If it isn't installed the
sampler logs one line and runs on the REST tail — it doesn't refuse to start.
