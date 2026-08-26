# Terminus prediction gaps: handoff notes

Status as of 2026-08-25. Written for whoever (human or Claude) picks
this back up.  The last section, on percentile ranges, is the most
recent work; the earlier sections are still accurate except that the
data set is now 16 days rather than one week.

## What we're trying to do

`nextbus.py` is a thin wrapper around the MBTA V3 API, deployed at
jefftk.com/nextbus/. At some stops it shows "No predictions." even
though a train is clearly on its way -- most painfully at Ball Square
inbound, one stop after the Green-E terminus at Medford/Tufts, where
the MBTA publishes nothing at all until the train is nearly there.

The goal is to show a useful estimate at those stops instead of
nothing. Two stops motivated this, and they turned out to need
completely different fixes:

- **Ball Square inbound (Green-E toward Copley)**: a real, severe
  prediction gap. Needs a fallback estimator. This is most of what
  follows.
- **Davis inbound (Red toward Ashmont/Braintree)**: *not* a prediction
  gap -- predictions are present 99.2% of the time. The actual problem
  is that the page doesn't say which branch each train goes to. See
  "Davis" below; it's a small, separate change.

The plan was: log real data, compare candidate fallback estimators
against what actually happened, pick the best. That's done, and the
result is now live in `nextbus.py` -- see "What's implemented" at the
end.

## READ THIS FIRST: the logged week is not normal service

**From Aug 8-16 2026 the MBTA suspended Green Line service** between
Government Center and Babcock Street (B), Government Center and
Cleveland Circle (C), North Station and Kenmore (D), and Heath Street
and North Station (E). Logging started Aug 11, so **six of the eight
logged days are shutdown-period data.** Only Aug 17 ET onward is
normal service.

Practical consequences:

- The E ran only Medford/Tufts <-> North Station for most of the week.
- Green-C barely appears in the data at all (9,960 vehicle-rows for
  the week vs 52k-92k for the other branches).
- Directions originating at downtown termini are unmeasurable anyway;
  see "Gotchas" below.

We checked whether this invalidates the Ball Square findings. It does
not -- Aug 17 is indistinguishable from the shutdown days, and the
fitted estimator tables reproduce almost exactly (details below). But
**anything resting on a single clean day should be re-verified**, and
if you refit on this data without knowing about the shutdown you will
draw wrong conclusions about the B/C/D branches.

## What's running

- `nextbus.py` -- the live site. Now shows an estimated range where
  the MBTA publishes nothing; see "What's implemented".
- `log_termini.py` -- logs raw `predictions` and `vehicles` responses
  for the whole subway/light-rail system to gzipped JSON-lines, one
  poll per 60s. Running as systemd service `log-termini.service` on
  jefftk.com (`ssh ps`), writing to `/home/jefftk/nextbus-logs`
  (gitignored). Continuously up since 2026-08-11 01:41 UTC with zero
  errors. ~50 MB/day; 936 MB so far against 5.5 GB free, so roughly
  three more months of headroom. There's an untested safety check that stops
  logging below 1 GB free.
  - Pull logs down with:
    `rsync -az ps:/home/jefftk/nextbus-logs/ ~/code/nextbus/logs/`
  - **Keep it running.** We now want a normal-service week to refit
    against.

## How to measure this correctly

Ground truth (when a train actually reached a stop) has to come from
vehicle positions, since predictions are exactly what's missing.
Getting this right matters more than it sounds -- two different
plausible-looking methods gave materially different answers.

**Do:** track vehicles by **vehicle id**, convert each vehicle's
current stop to an index into that direction's stop list, and count an
arrival the first poll at which the index reaches or passes the target
(treating INCOMING_AT/IN_TRANSIT_TO as "hasn't got there yet", so
position = index - 1). A train that slips entirely between two 60s
polls still gets counted.

**Don't:** key on `trip_id`, and don't detect arrivals by looking for
a vehicle reported STOPPED_AT the target platform. Both undercount,
and the trip-keyed version undercounts in a way that is correlated
with exactly the terminus behaviour being studied.

Score estimators **leave-one-day-out**. The candidate models are
median-per-bucket lookup tables, which will happily memorise a week.

## Ball Square: what's actually wrong

Green-E direction 0, platform 70510, terminus Medford/Tufts.
790 arrivals over 9,977 polls.

The MBTA publishes a prediction there in **5.2% of polls**, and is
silent 88.8% of the time when a train is really within 6 minutes and
72.3% of the time when it's within 3. Broken out by how far away the
next train really is:

| train truly N min away | silent |
|---|---|
| 0-2 min | 45.8% |
| 2-4 min | 99.5% |
| 4-6 min | 99.9% |
| 6-8 min | 100% |

On Aug 17 (normal service): 6.2% present, 99.6% silent at 2-6 min.
Same as the shutdown days.

Two findings that constrain what a fix can look like:

1. **Back-propagation is impossible.** For polls where a train arrives
   at Ball Square in 2-6 min, that trip has a prediction *anywhere on
   the line* only **0.3%** of the time. There is nothing to propagate
   backward from a downstream stop -- the MBTA simply publishes
   nothing for the trip until it's nearly at Ball Square.
2. **The schedule is useless for phase.** MAE 20.1 min. The Green Line
   runs almost entirely on dispatcher-ADDED trips (see below), so the
   static timetable has little relation to what actually operates.

For contrast, the same station's *outbound* platform gets predictions
up to 84 minutes out. Live spot check on 2026-08-17 21:01: Ball Square
had 10 outbound predictions and zero inbound, while two fresh inbound
trains sat at Medford/Tufts.

## Ball Square: what works

Leave-one-day-out, full week:

| estimator | MAE | median AE | p90 | false urgency* |
|---|---|---|---|---|
| live MBTA (when present, 5% of polls) | 0.8m | 0.8m | 1.0m | 0.2% |
| headway only | 3.4m | 1.6m | 8.0m | **29.6%** |
| vehicles only | 3.5m | 2.0m | 8.1m | 1.4% |
| vehicles + scheduled headway | 3.2m | 2.0m | 7.2m | 3.9% |
| vehicles + arrival-side predictions | 2.8m | 1.5m | 7.0m | 1.0% |
| **vehicles + live headway** | **2.8m** | **1.1m** | 7.0m | 1.0% |

\* fraction of times we'd display "<=3 min" when the train is actually
6+ min away. This is the metric that matters for not lying to people,
and it's where headway-only fails badly.

The vehicle lookup table, which is the core of it (full week; "queued"
counts direction-0 trains STOPPED_AT the terminus with a fresh
position):

| state | median wait | p25 | p75 |
|---|---|---|---|
| a train is INCOMING_AT Ball Square | 1.0m | 1.0 | 1.0 |
| a train is IN_TRANSIT_TO Ball Square | 2.0m | 2.0 | 2.0 |
| none in transit, 0 queued at terminus | 11.1m | 8.1 | 17.1 |
| none in transit, 1 queued | 7.1m | 5.0 | 10.1 |
| none in transit, 2 queued | 6.0m | 4.0 | 8.1 |
| none in transit, 3+ queued | 5.1m | 3.1 | 9.1 |

The first two rows are the single biggest win: a train that has
physically left Medford/Tufts arrives in 2 min, essentially
deterministically (~18s MAE -- better than the MBTA's own
predictions). Run time from the terminus is 2-3 min, so this covers
roughly the last 2 minutes and nothing beyond.

The queue rows carry the 3-5 minute horizon, and they're much
softer. Note they shift with service level -- weekday q=0 is 7-9 min
but weekend q=0 is 13-15 min -- which is why a service-frequency term
is needed alongside. Aug 17 (normal service) reproduced the weekday
numbers exactly (q=0 9.1m, q=1 5.1m, q=2 5.1m), so the table transfers
off the shutdown period.

Things that surprised us and are worth not re-deriving:

- **Dwell time at the terminus is nearly uninformative.** A train that
  has sat at Medford/Tufts for 20 minutes is no more likely to leave
  soon than one that just arrived. Median layover is 12.1 min (p25
  8.1, p75 16.1) and departures are not FIFO by dwell.
- **Vehicle count on the line doesn't help** as a proxy for service
  frequency (it overfits). Scheduled headway does work -- but only as
  a "how often do trains run at this hour" signal, *not* as a
  timetable. Don't let anyone "improve" it into schedule matching.

## Does the arrival side predict the departure side?

Worth reading before re-investigating: the idea that a long gap in
arrivals *into* Medford/Tufts should lengthen the gap in departures
out of it, since the MBTA tries to hold even headways.

It's real but narrow. Conditioned on how many trains are already
parked at the terminus (median wait until the next Ball Square
arrival):

| next predicted arrival at M/T | 0 queued | 1 queued | 2+ queued |
|---|---|---|---|
| 0-3 min | 8.1m | 6.1m | 5.0m |
| 3-6 min | 12.1m | 7.1m | 5.0m |
| 6-9 min | 14.1m | 7.1m | 5.0m |
| 9-12 min | 18.2m | 8.1m | 5.1m |

Strong and monotone when the terminus is empty; completely flat once
two trains are parked. The 12-minute median layover absorbs arrival
irregularity. Observed arrival gap vs observed next departure gap
correlates 0.574 overall, but the relationship is flat for every
arrival gap under 16 min -- the correlation lives in night/service-gap
regimes that the headway term already captures.

The empty-terminus state is only 11% of polls, and we could not
exploit it: adding the feature made things worse even gated to that
subset (5.4m -> 5.9m MAE there), because a week doesn't provide enough
samples to fit the extra dimension. **Worth retrying with more data**
-- it's the regime where the estimator is worst.

Its real value is architectural: used *instead of* stored headway
history it reaches the same 2.8m MAE with no persistent state, and
those predictions are already in the response `nextbus.py` fetches
today (`filter[stop]=place-balsq` returns the direction-1 predictions
at platform 70509 -- trains about to reach Medford/Tufts and turn
around). The arrival-side predictions themselves are good: MAE 2.0
min, median 1.1 min.

## Which stops have this problem

ADDED trips (dispatcher-created, not in the static schedule) cleanly
separate the Green Line from everything else, full week:

| route | vehicles on ADDED trips |
|---|---|
| Green-B | 85.7% |
| Green-C | 23.6% (barely ran; see shutdown) |
| Green-D | 87.1% |
| Green-E | 79.0% |
| Red | 15.8% |
| Blue | 10.0% |
| Orange | 9.6% |
| Mattapan | 4.5% |

But **ADDED does not explain severity within the Green Line** -- B, D
and E are all heavily ADDED and differ enormously. Don't over-apply
it.

Severity on Aug 17 (normal service), first stop after each terminus,
percent of polls silent when a train is truly 2-6 min away:

| stop | terminus | run time | silent @2-6min |
|---|---|---|---|
| Ball Square | Medford/Tufts | 3.0m | 99.6% |
| Englewood Ave | Cleveland Circle | -- | 94.8% |
| Woodland | Riverside | 2.0m | 69.3% |
| Lechmere | Union Square | 5.0m | 65.4% |
| South Street | Boston College | 8.0m | 1.7% |

The long run from Boston College is why South Street has warning: the
MBTA starts predicting a Green Line trip once the train departs its
terminus, so the free lookahead is roughly the terminus-to-stop run
time. That explains the good case but **not** the ordering among the
bad ones (Woodland has a shorter run than Ball Square yet is far less
silent), so don't treat it as a general model.

Everything except Ball Square rests on that single clean day, and
Englewood Ave rests on 70 arrivals. Re-verify before trusting.

**Mattapan behaves like heavy rail, not like the Green Line**, despite
being trolleys: predictions present 88-96% of polls, silent only
1.0-2.8% when a train is within 6 min, MAE 1.5-2.3 min. Being a
trolley line isn't the discriminator; running to a real timetable is.

## Davis

Red direction 0, platform 70063, 1,411 arrivals over the week.
Predictions present **99.2%** of polls, never silent when a train is
within 6 min, MAE 2.3 min (median 1.3, p90 4.5). Alewife's blind spot
does not reach Davis. **No estimator work needed here.**

The actual gap is that `nextbus.py` groups predictions by
`(route_id, direction_id)` and requests `include=vehicle,route,stop`,
so Davis renders one undifferentiated "Red Line: South" list. The
headsigns are right there in the API (Braintree 45,195 predictions,
Ashmont 39,737), and **89.5% of the time the next 12 minutes contains
both destinations**. Median wait for a specific branch is ~4 min but
p90 is ~11 min, so the distinction matters to a rider.

Fix: add `trip` to the include and label/group by headsign.

We could **not** establish the MBTA's absolute accuracy at Davis: 40%
of predicted trips couldn't be matched to an observed arrival (trip-id
churn), and vehicle positions lag ~25s median but 234s at p90, biasing
ground truth late. An early cut suggested systematic under-prediction;
we don't believe it. Treat as open, and don't try to answer it with
trip-matched comparisons.

## Gotchas that cost us time

1. **Trip ids get reassigned while a train lays over at a terminus.**
   Anything keyed on "first time we saw this trip = it departed the
   terminus" is corrupted. This is what made an earlier round of this
   analysis conclude that vehicle data was the *weakest* signal when
   it is in fact the strongest.
2. **`include=trip` does not sideload ADDED trips.** A vehicle whose
   trip id starts with `ADDED-` will have a trip relationship but no
   entry in the response's `included` array. Do not infer "no trip
   assigned" from a missing headsign -- read the relationship
   directly. (This burned us; the vehicles in question were normal
   trains.)
3. **Vehicle positions go stale.** Median age ~25s, but p90 is 142s
   (Green-E) to 234s (Red), and 6.8% of terminus observations are over
   5 minutes old, peaking at 28% during the 06:00 pull-out. Filter the
   terminus queue on `updated_at` freshness (<=5 min worked well).
   Filtering on "has an assigned trip" is a no-op -- they essentially
   always do.
4. **`/stops?filter[route]=X&filter[direction_id]=Y` returns only one
   branch pattern for branching routes.** For Red it returns only the
   Braintree pattern in both directions; Ashmont is absent. Use
   `/route_patterns` if you need all patterns. This doesn't affect
   Davis (it's on the shared trunk) but it will silently drop stops
   elsewhere.
5. **Directions originating at downtown termini can't be measured**
   -- most trips short-turn and never pass the nominal terminus, so
   there are only 73-91 detectable arrivals per week for Green-B/C
   direction 0 (Government Center) and Green-E direction 1 (Heath
   Street). This is an operational fact, not a detector bug. The old
   "Heath Street has no gap" reading was an artifact of this.
6. **Poll timestamps are UTC; log filenames are UTC dates.** Schedule
   times come back in US/Eastern. Convert before comparing, and bucket
   by ET day when splitting by service day, or you'll smear 8pm-midnight
   into the wrong day.

## Recommended next steps

1. **Ball Square fallback in `nextbus.py`.** Layered, and physically
   ordered: a direction-0 train in transit from Medford/Tufts (1-2
   min, near-certain) -> terminus queue size -> service-frequency
   term. Gate the queue count on position freshness. Start with the
   stateless shape: one extra `/vehicles?filter[route]=` call plus the
   arrival-side predictions already in the existing response. Show it
   as an estimate, visibly distinct from a real prediction.
2. **Davis branch labels.** Independent of the above and much smaller.
3. **Refit once there's a normal-service week**, then extend to
   Green-D (both ends) and Green-C. Green-B's outer end doesn't need
   it.
4. **Retry the empty-terminus arrival-side signal** with more data.
5. Decide when to stop `log-termini.service`.

## Note on the code

`analyze_termini.py` (in the working tree, never committed) is the
first-generation analysis script. It is **superseded**: it detects
arrivals via trip ids and STOPPED_AT-at-platform, which is the
undercounting method described above, and its headline conclusion
("headway wins") is an artifact of that plus the corrupted in_transit
estimator. Keep it for reference or delete it, but don't build on it.

`analyze_terminus_gap.py` (in the working tree, not yet committed) is
the second-generation script and the one to use. It implements the
measurement approach described above and regenerates the availability
figures, the vehicle lookup table, and the leave-one-day-out estimator
comparison for any stop:

    python3 analyze_terminus_gap.py Green-E 0 place-balsq
    python3 analyze_terminus_gap.py Mattapan 1 place-capst

It takes a route, a direction id and a parent station id, works out
the platform itself, reads from `NEXTBUS_LOG_DIR` (default `./logs`),
and needs network access to map platforms to parent stations. Give it
a few minutes -- it parses the whole log set. The Ball Square numbers
in this document are its output.

It does not yet cover the arrival-side features from the
"arrival side" section above, or the per-service-day splits used to
separate shutdown from normal service; those were done ad hoc.

## Percentile ranges instead of point estimates (2026-08-25)

Follow-up question: rather than "next train in N minutes", show the
**5th and 95th percentile** of the wait, so a rider walking to the
stop can tell whether hustling could pay off or whether nothing is
plausibly coming. Short answer: the p5 half works well and is cheap;
the p95 half is structurally harder and is only honest if it's allowed
to say ">45m".

Data is now 16 days (Aug 11-26), which covers the Green Line partial
shutdown (Aug 11-16), normal Green service (Aug 17+), and the Orange
Line partial shutdown (roughly Aug 19-26; it doesn't touch Green-E or
Red service levels).

### New code

- `analyze_terminus_quantiles.py` -- same data path as
  `analyze_terminus_gap.py`, but fits **quantile** lookup tables
  instead of median ones, with a **backoff chain** (specific bucket ->
  coarser bucket -> unconditional) so a sparse bucket falls back
  rather than inventing a tail from 30 samples. Scores **calibration**
  leave-one-day-out, overall, per vehicle state, and per hour.
  `SINCE=` / `UNTIL=` env vars restrict to a service period.
- `check_arrival_gaps.py` -- sanity-checks the arrival detector.
- `build_timeline()` in `analyze_terminus_gap.py` gained
  `precise_arrivals=` (default off, so existing numbers reproduce).

### Ground truth was biased late; fixed

Arrivals were timestamped at the **poll** time, but the position being
read was reported at `updated_at`, typically ~25s and sometimes
minutes earlier. Timing arrivals by the poll inflates every wait, and
for "should I hustle?" that is the dangerous direction -- it tells
people to relax when a train is about to go. Using `updated_at` moved
the Ball Square p5 for a train already in transit from 1.0m to **0.2m**,
and for `queued=1` from 3.0m to 2.3m. All numbers below use the fix.

### Calibration (leave-one-day-out, 16 days)

Ball Square, Green-E dir 0. "width" is the median p95-p5 spread:

| estimator | <p5 | >p95 | width |
|---|---|---|---|
| unconditional | 5.0% | 0.0% | 44.3m |
| headway only | 5.1% | 3.1% | 33.1m |
| vehicles | 5.1% | 5.0% | 15.5m |
| **vehicles + headway** | **5.1%** | **5.6%** | **8.6m** |

Davis, Red dir 0: vehicles+headway 5.1% / 4.0%, width 6.2m; the live
MBTA prediction bucketed by minute does about as well (4.6% / 4.6%,
6.0m), which matters because Davis has a prediction 99% of the time.

Two things to take from this:

1. **Headway alone is nearly useless here.** 33m width at Ball Square,
   44.5m at Davis -- barely better than saying nothing. The
   information is in the *vehicle state* (has a train left the
   terminus, how many are parked at it); headway is a useful second
   term, not the primary one. This mirrors the point-estimate result.
2. **Refitting on normal-service days only reproduces it**: Aug 17-26
   alone gives 5.1% / 5.1%, width 8.4m. The tables transfer across
   the shutdown boundary.

### The windows themselves

Ball Square (`>45m` means the clip, i.e. "nothing plausibly soon"):

| state | p5 | median | p95 |
|---|---|---|---|
| terminus empty | 4.7m | 15.5m | >45m |
| 1 parked at terminus | 2.3m | 6.0m | 17.5m |
| 2 parked | 1.8m | 4.9m | 13.0m |
| train left terminus | 0.8m | 1.5m | 2.0m |
| train INCOMING_AT Ball Sq | 0.2m | 0.7m | 1.9m |

Davis is tighter throughout (empty terminus 2.6m/6.7m/>45m; 1 parked
1.8m/4.0m/8.5m).

### Where it breaks

- **p5 is solid; p95 is not.** Per-state, `<p5` lands at 4.7-6.3%
  everywhere. `>p95` runs loose at 5.7-8.2% for the queued states and
  is 0.0% for the empty terminus only because it's clipped.
- **Late evening is where p95 fails.** Per-hour at Ball Square, `<p5`
  stays in 3.6-7.4% all day, but `>p95` hits **13.5% at 23:00** and
  12.6% at midnight. End-of-service is a censored regime and a finite
  p95 there is a lie; it needs to degrade to ">45m" or "last train
  may have gone".
- **The upper tail rests on the detector.** A missed arrival makes the
  wait jump to the train after it. Ball Square gaps are cleanly
  unimodal at 7-9m, but there's a small secondary bump near 19-20m
  (~3% of gaps) that is either missed detections or genuine
  late-evening headways. That's the same order as the p95 looseness
  above, so treat p95 as +/- a few minutes, not exact.
- **A live "1 min" prediction is not reliable in the upper tail**:
  p95 of 11.0m at Ball Square and 7.4m at Davis, both *worse* than
  the "2 min" bucket. Possibly held/cancelled trains, possibly
  detector error. Unexplained; don't build on it without checking.

### "Worth hustling?" is a better-posed question than p5

The literal decision is: you're W minutes out and could arrive 2
minutes sooner. That only helps if a train turns up in that 2-minute
window. Computed directly, at Ball Square:

- terminus empty: 8.9% for a 6-min walk
- 1-3 parked: 22-25% for a 6-min walk
- train already in transit: 0.3-7% for a 6-min walk (it'll be long
  gone), but ~33-36% for a 10-min walk

The catch: by a 15-minute walk every state converges to ~2/headway
(11-22%), i.e. **the vehicle state tells you nothing once your walk is
longer than the terminus-to-stop run time**. The useful range for this
display is short walks. Worth deciding whether to show this instead of
a raw percentile pair, since it answers the question directly.

### Effort

The statistics were ~1 hour: it's the same lookup tables with
`median()` swapped for an interpolated quantile, plus a backoff chain
and a calibration score. The parts that actually took work were the
`updated_at` timing fix and discovering the late-night p95 failure --
both invisible unless you score calibration per-slice rather than
overall.

## The MBTA largely fixed this on 2026-08-20

Spotted by noticing live inbound predictions at Ball Square that
weren't there two weeks earlier. It's real, it's a step change, and it
happened *inside* the logging window. Percent of polls where a train
is genuinely 2-6 minutes away and the MBTA says nothing:

| day | silent @2-6min | | day | silent @2-6min |
|---|---|---|---|---|
| Aug 11-18 | 99.6-100% | | Aug 22 (Sat) | 65.1% |
| Aug 19 | 99.8% | | Aug 23 (Sun) | 96.2% |
| Aug 20 | 56.6% | | Aug 24 (Mon) | 20.7% |
| Aug 21 (Fri) | 5.4% | | Aug 25 (Tue) | 2.1% |

### Two separate events, don't conflate them

1. **Aug 17: the shutdown ended.** Green Line service went back to
   scheduled trips -- ADDED fell from ~100% to 5-9% on every branch.
   This did **not** fix predictions: Aug 17-19 scheduled trips were
   still **99.9% silent** at 2-6 min.
2. **Aug 20: the MBTA changed something.** From Aug 21, per arriving
   train (`check_added_vs_silence.py`):

   | arriving train's trip | n | MBTA silent |
   |---|---|---|
   | scheduled | 1,487 | **3.3%** |
   | ADDED | 763 | **90.7%** |

   Before Aug 20 both were ~100% silent, so this is a genuine change
   in publishing behaviour, not a side effect of (1).

### What's left

The terminus blind spot was never really about the terminus -- it's an
**ADDED-trip** property, and now that's all that's left of it. The
MBTA still publishes nothing for dispatcher-added trips, which are:

- ~5-10% of weekday Green Line service (so weekdays are now fine),
- **66-100% of weekend service** (Aug 22 66% ADDED / 65% silent, Aug
  23 100% / 96%),
- 100% during shutdowns.

The daily correlation is almost 1:1 from Aug 21 on -- silent@2-6min
tracks that day's ADDED fraction to within a few points.

### Consequences

- **The fallback estimator is still worth having, but it's now a
  weekend-and-disruption feature, not an everyday one.** It self-gates
  correctly: it only fires when there are no predictions, which is now
  precisely the ADDED case.
- **The quantile tables remain valid.** They map vehicle state to a
  wait distribution, which is a physical property of how the service
  runs; it doesn't depend on whether the MBTA publishes predictions.
  The time-of-day term already carries weekend buckets, which is the
  regime that now matters most.
- **Re-check before doing more estimator work.** If the MBTA extends
  the fix to ADDED trips, this whole line of work becomes unnecessary.
  `check_gap_trend.py` and `check_added_vs_silence.py` answer that in
  one run each.
- Keep the logger running through at least one more weekend; weekend
  ADDED service is now the only regime that matters and it's the one
  we have the least clean data for.


## What's implemented

`nextbus.py` now shows an estimated *range* for a direction the MBTA
publishes nothing for, instead of rendering no section at all:

> **No MBTA predictions. Next train probably 2-15 minutes.**
> *Estimated because 3 trains are waiting at Medford/Tufts -- not an
> MBTA prediction. The wait falls outside this range about 1 time in
> 10.*

- `terminus_tables.py` is **generated** by `fit_terminus_tables.py`;
  don't hand-edit it. Regenerate after pulling fresh logs.
- Five stops qualify, all Green Line: Ball Square (E/Medford-Tufts),
  Englewood Ave (C/Cleveland Circle), Woodland (D/Riverside), Lechmere
  (D/Union Sq), South Street (B/Boston College). Everything else is
  skipped because the MBTA is silent <25% of the time when a train is
  within 6 minutes -- including Davis at 0.0%.
- It is fitted on **Aug 17 onward**, deliberately excluding the Green
  Line shutdown. Fitting across the shutdown taught the tables that an
  empty terminus means "no train for ages", which is true of a
  shutdown and false of ordinary service -- it made Green-C claim "2+
  minutes, possibly much longer" with an empty Cleveland Circle.
- Calibration on the shipped tables, leave-one-day-out and scored only
  on the polls where the MBTA is actually silent (the only population
  the estimator ever sees): `<p5` 5.1-6.2%, `>p95` 3.6-5.9%.
- Cost is one extra `/vehicles` call on an affected page view. Stop
  ordering is cached for an hour via the new `ttl=` argument to
  `mbta_get`, because the API allows only 20 requests/minute without a
  key. The whole estimate is wrapped in try/except: it must never be
  able to break the page.
- `service_period()` in `nextbus.py` and `period()` in
  `fit_terminus_tables.py` must stay identical, and both bucket by
  Eastern local time -- bucketing weekends by UTC date puts Friday
  evening in a weekend bucket.

Not done: the "worth hustling?" probability display, and the Davis
branch labels (still the highest-value remaining change, and
independent of all of this).
