# Engineering Code of Conduct — Smart Market Watchlist

This is not a style guide. It is the set of rules that govern how work
gets done in this repository, for any engineer — human or AI — touching
this codebase. It exists because this project has already been damaged
once by violations of rules 6, 7, and 22 below (silent price/volume
staleness from `fast_info`, a route path drifting from the documented
contract, a checkpoint-advancement test that quietly asserted the wrong
behavior). These rules are the direct result of real mistakes made and
fixed in this repository, not theoretical best practice.

If you are Claude, Claude Code, or any other implementation agent
working in this repo: read this file before touching anything. If a
task conflicts with a rule here, the rule wins unless the person
explicitly overrides it for that specific task.

## The 32 Rules

1. **Correctness over complexity.** A correct, boring solution beats a
   clever, fragile one. This project chose on-demand fetches over a
   background poll loop for exactly this reason, and documented it as a
   deliberate deviation in `decisions.md` rather than silently drifting.

2. **Inspect existing code and architecture before changing anything.**
   Read `plan.md`, `architecture.md`, `decisions.md`, and the actual
   current implementation of anything you're about to touch. Do not
   assume you remember correctly — this repo has already caught bugs
   from stale assumptions (the `fast_info` staleness issue was found
   because someone actually ran the code and compared it to what the
   library's source said, not because it looked right).

3. **Make the smallest coherent change that solves the actual
   problem.** The `previous_close` fallback fix touched three lines of
   real logic. It did not become an excuse to restructure the provider,
   rename unrelated methods, or "clean up while we're in there."

4. **Preserve architectural boundaries.** `YFinanceProvider` is the only
   module that imports `yfinance`. Nothing else may import it directly,
   ever, regardless of how convenient a shortcut looks. If you find
   yourself wanting to reach around `MarketDataProvider`, `CheckpointService`,
   or `MarketDataService` to touch raw data, that is a signal to stop and
   reconsider, not a signal to add an exception.

5. **Business invariants take priority over implementation
   convenience.** If a schema-level `pydantic` validator makes an
   endpoint slightly more annoying to call, the validator stays. The
   `MarketSnapshot.percent_change` self-check exists specifically because
   it is cheaper to enforce "never trust a provider percent field" in
   code than to trust every future caller to remember it.

6. **Separate market observation from user acknowledgement/checkpoint
   state.** A `MarketSnapshot` is what the market is doing right now. A
   `Checkpoint` is what a specific user has acknowledged. These are not
   the same kind of fact and must never be merged into one document or
   one code path. The frozen-copy design (`Checkpoint.baseline_snapshot`)
   exists because `MarketSnapshot` is mutable/overwritten and a
   checkpoint must not be.

7. **Never silently advance user state because of page loads,
   rendering, polling, refreshes, or stale data.** `GET /watchlist` may
   *establish* a first-time implicit checkpoint (because none exists
   yet), but it must never *replace* an existing one. If you are writing
   a read endpoint and it ends up calling anything that looks like
   `create_checkpoint_from_snapshot`, stop — that is a write operation
   masquerading as a read.

8. **Invalid, unavailable, or insufficient-quality market data must not
   create or advance user state.** A `503` on `mark_as_seen` when no
   valid snapshot exists is correct behavior, not a bug to smooth over.
   Never fabricate a checkpoint from a failed fetch to make an endpoint
   "succeed."

9. **Keep Change Detection separate from Attention Ranking.** The
   Change Engine decides *whether* something changed and *why*, using
   fixed thresholds. A future Attention Engine decides *what order* to
   show changes in. These are different questions with different
   failure modes; do not fold ranking logic into the Change Engine or
   vice versa.

10. **Keep deterministic business logic deterministic. Do not introduce
    LLMs where ordinary code is sufficient.** The 2.0% price threshold
    and 2.0x volume-acceleration threshold are plain `if` comparisons on
    purpose. A judge or user must be able to verify the result against
    the raw numbers without trusting a model's output.

11. **Every important state transition must be explicit and
    understandable.** `CheckpointSource.EXPLICIT` vs.
    `CheckpointSource.IMPLICIT` exists so that anyone reading a
    checkpoint document can tell how it got there without archaeology.

12. **Prevent duplicate logical ChangeEvents from repeated
    refreshes/polling.** A `ChangeEvent` is tied to the specific
    checkpoint it was detected against and must not be recreated every
    time `GET /watchlist` runs. If this repository's `ChangeEvent`
    persistence layer is built without this guarantee, it is not done,
    regardless of what else works.

13. **Comments should explain WHY, not restate WHAT the code already
    says.** `# add 1 to i` is noise. `# rate_before divides by
    minutes_since_open_to_checkpoint; guard against near-zero to avoid a
    wild ratio` is the kind of comment this repo wants.

14. **Code should use meaningful domain names and read naturally to a
    human engineer.** `checkpoint_at`, `session_date`,
    `volume_acceleration_ratio` — not `ts2`, `d`, `vr`.

15. **Avoid AI-slop: no unnecessary abstractions, helpers, wrappers,
    dependencies, boilerplate, speculative infrastructure, or excessive
    comments.** A `yfinance_ticker()` helper method was added to
    `Instrument` and then deliberately removed in the same session
    because it was speculative — built for a phase that hadn't started
    yet, not for a problem that existed. That is the standard: if you
    can't point at the current problem it solves, don't add it.

16. **Error handling must not silently hide programming errors or
    corrupt state.** `YFinanceProvider.get_quotes()` never raises — but
    every failure carries a real, specific `error_message`, not a
    generic "something went wrong." Swallowing an exception into
    `None` with no trace of what happened is a bug, not error handling.

17. **Tests are part of the implementation, not an afterthought.** A
    change without a test proving it is not considered complete in this
    repository, regardless of how obviously correct it looks.

18. **Test happy paths, boundaries, failure modes, and regressions.**
    The 2.0% and 2.0x thresholds are inclusive (`>=`); the boundary
    itself (exactly 2.0) is tested explicitly, not just values clearly
    above or below it.

19. **Run focused tests and the relevant full test suite after
    meaningful changes.** Focused first for fast feedback, full suite
    before considering anything done — this repo has caught real
    regressions (a stale test asserting old behavior) only because the
    full suite was run, not just the file under active work.

20. **Manually verify important user-facing flows.** Passing tests are
    necessary, not sufficient. The checkpoint flow was only confirmed
    correct after someone actually ran the server and issued real
    `curl` requests against it, twice — the first automated pass missed
    that `previous_close` was silently `None` in production data.

21. **Keep architecture documentation consistent with actual
    implementation.** If the code does something `architecture.md`
    doesn't describe, that's a documentation bug, not a detail to skip.
    The "on-demand fetch instead of a background poll loop" gap was
    written up as an explicit, named deviation in `decisions.md` the
    moment it was noticed — not left implicit.

22. **Do not silently change the semantics of existing fields,
    formulas, timestamps, thresholds, or states.** `fetched_at` is
    ours; `provider_timestamp` is diagnostics-only and never
    authoritative for freshness. If that boundary ever needs to move,
    it needs a decision entry explaining why, not a quiet edit.

23. **Do not rewrite working systems without a concrete engineering
    reason.** The intraday-history provider revision happened because
    of direct, reproduced evidence that `fast_info` was stale on the
    real runtime — not because history-based sourcing seemed more
    elegant.

24. **Do not add infrastructure such as Kafka, Redis, Kubernetes,
    WebSockets, queues, microservices, etc. without an actual
    demonstrated requirement.** This project runs one FastAPI process,
    one MongoDB, one React frontend. Every time a heavier tool was
    considered, the actual bottleneck it would solve was named first —
    and in every case so far, none existed yet.

25. **Do not optimize without evidence.** Sequential per-symbol
    provider calls were kept, not batched, because five symbols fetch
    fast enough that batching would be solving a problem that hasn't
    been measured, let alone shown to exist.

26. **Never commit secrets, credentials, .env files, virtual
    environments, caches, or generated artifacts.** `.env` is
    documented via `.env.example` only. `venv/`, `__pycache__/`,
    `.pytest_cache/`, and `node_modules/` do not belong in version
    control.

27. **Use small, meaningful Git commits representing coherent
    engineering milestones.** One commit for the Checkpoint Service, a
    separate commit for the Meaningful Change Engine — not one giant
    commit mixing unrelated milestones because they happened to be
    built back-to-back without an intermediate commit. When a diff
    turns out to straddle two milestones, split it before committing
    rather than commit it as one blob and explain the mixture later.

28. **Before committing, inspect `git status`, `git diff`,
    `git diff --check`, and run relevant tests.** Every commit in this
    repository's history is expected to have gone through this
    sequence, no exceptions for "small" changes.

29. **Documentation must describe what the system actually does,
    including intentional deviations from the original plan.**
    `decisions.md` records the on-demand-fetch deviation, the swapped
    `previous_close` fallback order, and the rejected volume-ratio
    formula — not just the decisions that turned out clean on the first
    try.

30. **If a requirement is ambiguous or conflicts with an existing
    architectural decision, stop and surface the ambiguity rather than
    inventing behavior.** Silently picking an interpretation and moving
    on is how undocumented, contradictory behavior accumulates.

31. **Do not claim something is production-ready without evidence.**
    "All tests pass" is evidence. "This should work" is not. If manual
    verification hasn't happened, say so explicitly instead of implying
    it has.

32. **The human developer remains the final decision maker. You are the
    implementation agent, not the product owner.** Recommend, flag
    trade-offs, push back with reasoning — but do not silently make
    product, architecture, or scope decisions on the person's behalf.

## The Required Workflow

```
INSPECT
  → UNDERSTAND CURRENT BEHAVIOR
  → IDENTIFY INVARIANTS
  → DESIGN SMALLEST CHANGE
  → IMPLEMENT
  → TEST
  → ADVERSARIAL REVIEW
  → FIX
  → VERIFY
  → DOCUMENT
  → COMMIT
```

Do not skip steps because a change looks small. The `previous_close`
bug looked like a three-line fix and still needed real diagnostic
evidence, a regression test, and a documented decision before it was
actually done. Skipping ADVERSARIAL REVIEW is how the `fast_info`
staleness issue would have shipped unnoticed if nobody had asked "does
this actually refresh, or does it just look like it does."

## Human-Readable Code

Comments in this repository are sparse and purposeful. Most code should
be self-explanatory from naming and structure alone. Write a comment
when — and only when — it captures one of these:

- **Business reasoning**: why a threshold, rule, or behavior exists,
  especially when it traces back to a product decision (e.g., "never
  trust a provider percent field" next to a self-computed
  `percent_change`).
- **Invariants**: a condition that must always hold, particularly where
  violating it silently would corrupt data or mislead a user (e.g., "a
  ratio must never be attached when the signal is marked unavailable").
- **Provider quirks**: undocumented, surprising, or version-specific
  behavior of an external dependency that isn't obvious from reading
  the call site (e.g., why `fast_info.get("last_price")` looks correct
  but silently returns `None` on every call).
- **Failure handling**: what happens when something goes wrong and why
  that specific response was chosen over the alternatives.
- **Non-obvious trade-offs**: a deliberate simplification or rejected
  alternative that a future reader might otherwise "fix" by
  reintroducing the original problem.

Do not write a comment that just restates the line below it. If a
function, variable, or class name can't communicate its own purpose,
that's usually a signal to rename it, not to comment around it.

## AI-Assisted Development

AI-generated code is acceptable in this repository. Blindly generated
code is not. The distinction is not about the tool — it's about whether
a human (or, when working autonomously, the agent itself before
presenting the result) has actually verified the following before any
generated implementation is considered real work:

- **Understood**: could you explain what this code does and why, to
  someone reviewing it, without re-reading it first?
- **Reviewed**: has it been checked against this file's 32 rules, not
  just checked for syntax correctness?
- **Tested**: does it have tests that would fail if the logic were
  wrong, not just tests that happen to pass?
- **Justified against this repository's architecture and business
  rules**: does it fit `plan.md` and `architecture.md`'s actual design,
  or does it quietly introduce a new pattern that contradicts them?

Generating a large volume of plausible-looking code is not the goal.
Generating code that someone can defend, line by line, during review or
under questioning is the goal. If an implementation can't be defended
that way, it isn't finished, regardless of whether it runs.

## Definition of Done

A change is done when all of the following are true:

- [ ] The actual problem is understood and stated, not assumed.
- [ ] Existing code, tests, and documentation relevant to the change
      have been read, not just skimmed.
- [ ] The change is the smallest coherent one that solves the real
      problem — no speculative extras, no unrelated cleanup bundled in.
- [ ] No architectural boundary was crossed without a documented reason
      (e.g., a new direct dependency on `yfinance` outside
      `YFinanceProvider`).
- [ ] Relevant business invariants still hold, and at least one test
      would fail if they didn't.
- [ ] Focused tests for the change pass.
- [ ] The full relevant test suite passes (or every failure is
      understood and pre-existing/unrelated, not newly introduced).
- [ ] `git status`, `git diff`, and `git diff --check` have been
      inspected before staging anything.
- [ ] Important user-facing flows touched by the change have been
      manually verified, not just covered by automated tests.
- [ ] Any intentional deviation from `plan.md` / `architecture.md` is
      written into `decisions.md`, not left implicit.
- [ ] No claim of "production-ready," "fully robust," or similar is
      made without the evidence to back it up.
- [ ] Commits are small and represent one coherent milestone each, not
      a bundle of unrelated work.
- [ ] Nothing in `.gitignore`'s scope (secrets, `.env`, `venv/`,
      caches, generated artifacts) is staged.
