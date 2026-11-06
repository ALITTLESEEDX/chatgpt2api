# 0002: Image generation latency

Status: pending-verification

- Owner: unassigned
- Created: 2026-11-06
- Updated: 2026-11-06
- Related issues/ADRs: none

## Guiding constraint

Stability comes first. Each change must keep the current success rate,
failure classification, account switching, and upstream-visible behaviour
unchanged; latency is reduced only inside that envelope. A change that saves
time but can turn a success into a failure is not accepted without a matching
mitigation.

## Problem and user outcome

A successful single image takes 46.2 s at p50 (p90 60.5 s). Roughly half of
that time is local waiting, not upstream rendering. Users should receive the
same images with the self-inflicted waits removed.

## Current behavior and evidence

Production sample: 219 Call Records / 280 Image Attempts over 24 h
(2026-11-06), stage metrics from `detail.image_attempts[].monitor.metrics`
plus container logs.

| Stage (successful attempt, p50) | Time | Nature |
| --- | --- | --- |
| Account preflight (`account_wait_ms`) | 1.7 s | local, remote check per selection |
| Reference upload, edits only (`upload_ms`) | 2.4 s (p90 7.3 s) | serial per image |
| bootstrap + requirements + prepare | 1.6 s | partly overlappable |
| Generation start TTFB | 3.2 s | upstream |
| SSE close after terminal event (`sse_last_gap_ms`) | ~10 s | **defect** |
| Initial poll wait + settle (`poll_wait_ms`) | 10.7 s | fixed sleeps |
| Poll requests (`poll_request_ms`) | 2.3 s | serial task + conversation reads |
| resolve / download / store | 0.8 / 0.75 / 1.2 s | normal |
| Hide-conversation PATCH after success | 5.6 s (p90 6.7 s) | synchronous, not needed for the result |

Verified causes:

- **SSE close blocks (P0-1).** Every call logs `image_stream_terminal_break`
  3–5 s into the stream, yet `sse_stream_ms` sits at ~15 s. Leaving the loop
  closes the generator; `iter_sse_payloads` (`utils/helper.py`) then calls
  `response.close()`. In curl_cffi 0.15 `Response._finalize_stream()` sets
  `quit_now` and then waits on `stream_task.result()`. The curl worker only
  checks `quit_now` when the next chunk arrives, and upstream stays silent for
  ~10 s after the terminal event, so the caller waits for upstream to hang up.
  The existing timeout abort (`_abort_stream_nonblocking`) avoids the wait but
  calls `curl.close()` while the worker thread may still be inside
  `curl_easy_perform`, which is unsafe.
- **Synchronous conversation cleanup (P0-2).**
  `_cleanup_image_conversations_after_success`
  (`services/protocol/conversation.py`) issues the hide PATCH before the
  attempt returns; logs show 5.6 s p50 between `image_single_stream_done` and
  `image_single_done`.

- **Settle never changed the result (P0-3).** In the available production
  logs, 24 of 24 `image_poll_hit_pending_settle` events were followed 5 s later
  by `image_poll_hit` with identical file ids. The settle wait costs ~5 s plus
  one extra poll round on every success.

Unknown: the exact moment upstream finishes rendering. Unknown: whether the
image tool message is already `finished_successfully` at first sighting; if it
is not, the poller falls back to the existing settle path and logs
`record_statuses` on `image_poll_hit_pending_settle` so this can be checked. Data shows the first
poll after SSE close usually hits, so rendering ends ≤ ~21 s after generation
starts, but this was not measured directly.

## Scope

Ordered by priority. Status is tracked per item.

| Item | Change | Expected saving | Status |
| --- | --- | --- | --- |
| P0-1 | Close image SSE streams without waiting for upstream; defer curl handle close to the worker's completion | ~10 s per attempt | deployed, pending 24 h verification |
| P0-2 | Run the hide-conversation PATCH in a bounded background runner that owns the backend lifetime | ~5.6 s per success | deployed, pending 24 h verification |
| P0-3 | Poll cadence: return as soon as every found file id sits in a `finished_successfully` message (code); production settings `image_poll_initial_wait_secs` 3, `image_poll_interval_secs` 2, `image_settle_secs` 2 | ~6–8 s | deployed, pending 24 h verification |
| P1-4 | Skip the remote account preflight when the local account check is fresh | ~1.5 s | deferred: can turn a 1.6 s saving into a failed attempt + switch when an account just ran out of image quota; needs data on how often the preflight rejects accounts |
| P1-6 | Studio polls image tasks every 1.5 s instead of 4 s (`studioImageTaskRuntime.ts`); file tasks keep 4 s | ~1.3 s average visible delay | deployed `fde8442`, pending 24 h verification |
| P1-7 | Overlap the account preflight with bootstrap/requirements | ~1.3 s | deferred: needs egress reservation and bootstrap before the account is confirmed, a gate before the generation POST, and a new candidate-rejected path in the attempt loop that owns slots, quota classification, token rotation, and deadlines |
| P1-5 | Upload multiple reference images concurrently on the shared session (curl_cffi keeps a curl handle per thread and a locked cookie jar); single image unchanged | multi-image edits: 2 images ~2 s, 5 images ~5 s (p90 ~9 s) | deployed `e966d11`, pending 24 h verification |

P0-1 and P0-3 interact: once the stream closes ~10 s earlier, polling starts
earlier, and the fixed 5 s cadence absorbs part of the saving until P0-3 lands.

## Non-goals

- Hedged/duplicate generation on a second account (former P2): costs quota and
  changes upstream-visible behaviour.
- Changing failure classification, account switching rules, or retry limits.
- Reordering or parallelising the per-round task and conversation reads: saves
  ~0.6 s but rewrites a failure-merging loop with many paths; not worth the risk.
- Changing setting defaults in `contracts/settings_specification.py`; only the
  production values change.
- Changing Image Task, Call Record, or monitor contracts. Metric names stay the
  same; only their values shrink.

## Domain language

Uses existing terms: Image Task, Image Attempt, Upstream Account, Call Record.
No new term.

## Unique owners

| Concern | Authoritative owner | Interface consumed by others | Forbidden mirror or fallback |
| --- | --- | --- | --- |
| Streaming response shutdown | `close_stream_response` in `utils/helper.py` | called by `iter_sse_payloads` and stream owners | ad-hoc `curl.close()` on a live stream |
| Post-success conversation hiding | `_cleanup_image_conversations_after_success` in `services/protocol/conversation.py` | returns whether it took ownership of the backend | callers closing a backend they handed off |
| Attempt backend lifetime | the attempt loop in `_generate_single_image` | `backend.close()` in its `finally`, skipped only after hand-off | — |

## Backend Modules and Interfaces

- `utils/helper.py`: new `close_stream_response(response)`. curl_cffi streams:
  mark closed, set `quit_now`, close the curl handle from
  `stream_task.add_done_callback`. Other responses: plain `close()`.
  `iter_sse_payloads` uses it on every exit path, including the timeout abort.
- `services/openai_backend_api.py`: `_stream_picture_conversation` uses the
  same helper in its `finally`.
- `services/protocol/conversation.py`: cleanup submits to a module-level
  `BoundedTaskRunner` (existing house runner) and returns `True` when it took
  ownership of the backend; the background task hides each conversation and
  then closes the backend. If the runner is full, cleanup runs synchronously as
  today.

No public contract changes.

## Frontend interaction and responsive behavior

None.

## Persistence impact

None.

## Concurrency, security, and failure behavior

- P0-1: after an early close, the curl worker thread lingers until upstream
  sends a byte or hangs up (observed ~10 s; bounded by the request timeout).
  Previously the request thread waited for the same event. Upstream sees the
  same connection behaviour as before.
- P0-2: background PATCH runs outside the image egress reservation (it is a
  small request). Failures are logged exactly as before. Queued cleanups are
  lost on process restart; those conversations stay visible in the upstream
  history, which has no effect on results. Runner overflow falls back to the
  current synchronous path.
- P0-3: an immediate hit requires every file id to appear in a message whose
  status is exactly `finished_successfully`; `finished_partial_completion` and
  any other status keep the settle check. Shorter cadence raises conversation
  reads per image (~2 s apart); `image_poll_retry` events are the 429 signal to
  watch after deploy.
- P1-5: parallel uploads open up to 5 connections through the same egress
  for one request, as the web client does. Upstream limits uploads per account
  by count (429 `upload_protection_limit_exceeded`, 4 h window, reached by a
  ~40-upload benchmark in 10 minutes); parallelism does not change the count.
  `tests/test_image_upload_parallel.py` pins the per-thread curl handle
  assumption. Results are collected in
  image order, so the first failing image raises the same error the serial
  loop raised; sibling sessions are always closed. Overlapping uploads with
  bootstrap/requirements and caching bootstrap were dropped: both would share
  or skip the session that receives Cloudflare cookies, for ~1 s.
- Production settings applied 2026-11-06 via `PATCH /api/settings`:
  `image_poll_timeout_secs` 75, `image_poll_initial_wait_secs` 3,
  `image_poll_interval_secs` 2, `image_settle_secs` 2.
- **Poll window shift.** The poll budget (`image_poll_timeout_secs`, effective
  60 s in production) starts when the SSE phase ends. P0-1 moves that start
  ~10 s earlier, shrinking the absolute tolerance for slow renders by ~10 s.
  The slowest success in the sample needed ~56 s of the window. To keep
  tolerance unchanged, raise `image_poll_timeout_secs` to 75 at deploy.

## Acceptance criteria

- [ ] `sse_last_gap_ms` p50 drops from ~10 s to < 1 s; `sse_stream_ms` tracks real event time.
- [ ] Time from `image_single_stream_done` to `image_single_done` drops from ~5.6 s to < 0.2 s for successes.
- [ ] `image_conversation_removed` still logs for each successful conversation.
- [ ] Success, text_review, and `image_poll_timeout` rates over 24 h stay within the baseline range.
- [ ] No growth in live thread count after a load period (threads return to idle).
- [ ] `image_poll_hit_final` appears for successes and `poll_wait_ms` p50 drops below 5 s.
- [ ] `image_poll_retry` (429) frequency does not rise above baseline.

## Verification matrix

| Acceptance criterion | Verification level | Evidence required | Status |
| --- | --- | --- | --- |
| Early close does not wait for upstream | unit | `tests/test_image_latency_p0.py::test_early_exit_does_not_wait_for_silent_upstream` passes; the same test against the previous code blocks for the full 3 s stall | passed |
| Cleanup hand-off and fallback | unit | `tests/test_image_latency_p0.py` cleanup tests (background, full runner, disabled) pass | passed |
| Status-based hit, partial status excluded, settle fallback | unit | `tests/test_image_latency_p0.py` poll tests pass | passed |
| Existing behaviour | regression | `uv run --frozen pytest`: 36 passed, 1 failed (`test_web_image_model_slug_mapping`: stale expected model list after the v3.2.4 merge added `gpt-image-2.6`; slug mappings verified correct by hand) | passed |
| Production latency (early) | manual | 2026-11-06 after deploy `cb6519c`: 10 direct `/v1/images/generations` calls (2.6-sunburst, 2.5) all 200 in 17.2–23.1 s (baseline p50 41–44 s); `sse_last_gap_ms` ≈ 0; every hit via `image_poll_hit_final`; total − stream ≈ account wait (cleanup off path); 2 Studio tasks 200 / success in ~30 s right after restart (cold solver: `requirements_ms` 4 s) | passed |
| Parallel upload order, concurrency, failure, shared-session threading | unit | `tests/test_image_upload_parallel.py` (4 tests) pass | passed |
| Parallel upload against upstream | manual | out-of-band process in the production container, not deployed: 3 × 1.4 MB images on a fresh account: serial 1.7–2.1 s vs parallel 0.63–0.76 s (back-to-back bursts on one account slowed as the upload limit approached); full 3-reference edit (`gpt-image-2.5`) succeeded twice with upload stage 0.95–1.59 s (production 3-image p50 3.3 s) | passed |
| P1-5 / P1-6 after deploy | manual | 2026-11-07 02:15 BJ, 5 parallel requests right after restart, all success: 2 API generations 22.1 s, API edit with 3 refs 22.4 s (first attempt hit the upload-throttled account → `file_upload_throttled` → switched; upload 0.89 s), Studio edit with 2 refs 23.0 s (upload 0.51 s), Studio generation 31.0 s (upstream TTFB 9.0 s); deployed Studio bundle has `pollIntervalMs:1500`; no 5xx or tracebacks. First requests after a restart also pay ~3.5 s for the tiktoken encoding download (cache in container `/tmp`) and 2–3 s cold solver `requirements`; both predate this work | passed |
| Production rates over 24 h | manual | success / text_review / `image_poll_timeout` / `image_poll_retry` rates vs baseline | pending |

## Documentation and CHANGELOG impact

- `CHANGELOG.md`: Unreleased entries added for P0-1, P0-2, and P0-3.
- `docs/upstream-sse-conversation.md`: no change (parsing rules unchanged).

## ADR requirement

Not required: no ownership, persistence, or contract change.

## Rollback

Revert the commit. The production settings changed at deploy can be restored
independently in Settings: `image_poll_timeout_secs` 60,
`image_poll_initial_wait_secs` 5, `image_poll_interval_secs` 5,
`image_settle_secs` 5.

## Unresolved questions

- Production has two values for some image settings (`image_account_concurrency`
  1 in the Application Database vs 3 in `config.json`; poll timeout 60 effective
  vs 120 in `config.json`). Owner: user. Which source is intended?
