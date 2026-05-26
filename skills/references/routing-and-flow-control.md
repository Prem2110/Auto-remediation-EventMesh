# Routing & Flow Control: Splitter, Aggregator, Router, Multicast, Gather

For when failure involves flow control steps (splitter, router, aggregator, multicast, gather, looping process call).

---

## Routers

### Content-based router (Router step)

Routes based on header/property/body content. Common issues:

- **Default route missing** → unmatched messages fail with "no route matches." Always configure a default.
- **Mutually non-exclusive conditions** → router takes first match; intent may be different.
- **XPath against non-XML body** → silent failure. Use the right expression type.

### Exclusive vs non-exclusive

Router step is exclusive (first match wins). Multicast is non-exclusive (all matching branches execute).

---

## Splitter

### Iterating Splitter

Processes sub-messages sequentially. Use for ordered processing where downstream needs sub-messages in source order.

### General Splitter

Parallel processing of sub-messages. Use for high-throughput where order doesn't matter.

### Common splitter issues

- **`SplitComplete=false` left as default** → caller doesn't know if all sub-messages succeeded. Set `true` for "all-or-nothing" semantics, or handle partial failures explicitly.
- **One bad sub-message blocks aggregator** → in iterating mode, single failure halts flow. Either handle exceptions per sub-message (continue on error), or accept halt and resume from DLQ.
- **Memory pressure on large source** → splitter loads source to determine split points. Use streaming for large XML/JSON.
- **Splitter expression mismatch** → XPath doesn't match any element → 0 sub-messages → silent success with no downstream processing. Validate the split count in MPL.

### Splitter-aggregator pair

If a splitter is followed by an aggregator expecting N sub-messages, count mismatch causes aggregator to time out.

Symptoms: `AGGREGATOR_TIMEOUT` errors hours/days after the message entered.

**Fix**: align splitter output count with aggregator's completion condition.

---

## Aggregator

### Completion conditions

Aggregator combines sub-messages based on:
- **Size**: collect N messages
- **Timeout**: collect for T duration
- **Expression**: custom condition based on collected state

Most failures: condition never met.

- Size N, but splitter produced N-1 → wait forever (until timeout)
- Timeout T set too short → premature completion, lost messages
- Expression evaluates against wrong context → never true

### Correlation expression

Determines which sub-messages aggregate together. Default: same correlation = same message group.

Common bug: correlation expression returns null for some sub-messages → all "null" sub-messages aggregate into a single accidental group.

### Aggregation strategy

Built-in strategies: combine bodies, last wins, XML concatenation. Custom strategy via Groovy is common — and a common bug source.

Custom strategy pitfalls:
- Not handling first arrival (initial state setup)
- Mutating shared state (concurrency bugs)
- Throwing exceptions (aborts aggregation)

---

## Multicast

Sends the same message to multiple branches. Branches execute in parallel or sequentially.

Common issues:
- **One branch fails, others continue** → "Stop on Exception" is off by default. Decide intent explicitly.
- **Branches modify same headers/properties** → race conditions in parallel mode.
- **Aggregation strategy required** if expecting combined output downstream.

---

## Gather

Pairs with Multicast — combines outputs of parallel branches. Same aggregation strategy considerations as Aggregator.

---

## Looping Process Call

Calls a sub-flow repeatedly with a condition. Failure modes:

- **Condition never false** → infinite loop, eventual timeout/OOM
- **Loop body has side effects** → if condition depends on side effect, may loop wrong number of times
- **Counter increments not propagated** → check property scope; locals reset per iteration

---

## Sequential vs parallel processing

CPI's default for splitter is sequential (Iterating Splitter). Switching to General Splitter (parallel) requires:
- Thread-safe downstream steps (no shared mutable state)
- Idempotent receiver calls (parallel calls may interleave)
- Aggregator if order matters downstream

---

## Error handling within flow control

Exceptions inside splitter/multicast/aggregator behave differently than outside:

- **Iterating splitter**: exception in one sub-message can halt the splitter or be caught per-sub-message (configurable via "Stop on Exception").
- **General splitter**: parallel sub-messages — other branches may still be executing when one fails.
- **Multicast**: similar to splitter; configurable stop-on-exception.

Exception subprocess at iFlow level catches what propagates out of these constructs, but the message context (which sub-message failed, what was in flight) is often lost. Use local error handling within the splitter when sub-message context matters.

---

## Throughput tuning

- General Splitter parallelism is bounded by tenant worker threads. Increasing splitter parallelism on a worker-constrained tenant just queues internally — doesn't improve throughput.
- For high-fan-out scenarios (one message → 10,000 sub-calls), consider JMS-based decoupling rather than in-process splitter.

---

## Error signatures

| Pattern | Meaning |
|---|---|
| `Camel Exception` + `splitter` | Sub-message failure |
| `Camel Exception` + `aggregator` + `timeout` | Completion condition never met |
| `No route matches` | Router default route missing |
| `Multicast: 1 of N branches failed` | Partial failure in multicast |
| `Loop iteration limit exceeded` | Looping process call hit safety limit |
