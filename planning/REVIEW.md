# Change Review

## Findings

### [P1] Discard the Stop-hook payload before invoking Codex

**File:** `independent-reviewer/hooks/hooks.json:8`

Claude Code supplies the Stop event JSON on the hook command's standard input. `codex exec` consumes piped standard input as additional prompt content even when a positional prompt is present. Consequently, session-controlled content such as `last_assistant_message` is appended to the review request. This defeats the review's independence and creates a prompt-injection path that can cause the reviewer to omit findings or make unintended workspace edits. The invocation that generated this review demonstrates the issue: the complete Stop payload appears as a `<stdin>` block after the fixed request.

Close standard input when launching the reviewer:

```json
"command": "codex exec \"Review changes since last commit and write results to a file named planning/REVIEW.md\" </dev/null"
```

If hook metadata is needed later, use a wrapper that parses only explicitly required fields and never forwards session text to Codex.

### [P1] Use nanoseconds for the snapshot trade timestamp

**File:** `planning/MASSIVE_API.md:76`

The example labels `snap.last_trade.timestamp` as Unix milliseconds, but the same document's response schema and field table correctly identify the v2 snapshot trade timestamp as Unix nanoseconds (`lastTrade.t`). This is not cosmetic: following the milliseconds claim leads to dividing by `1_000`, as the current `MassiveDataSource` does, and produces timestamps one million times too large. Consumers then receive dates far outside the valid Unix range. Document the value as nanoseconds and convert it to seconds with `/ 1_000_000_000` (and update the implementation before relying on this reference).

### [P2] Replace the invalid unified-snapshot Python example

**File:** `planning/MASSIVE_API.md:169`

The `/v3/snapshot` example calls `client.list_snapshot_chain(...)` and passes an underlying ticker, which describes an options-chain operation rather than the unified multi-asset snapshot documented in this section. The official client exposes the options operation as `list_snapshot_options_chain`; this example is neither that valid method name nor a request for `/v3/snapshot`, so copying it will fail or target the wrong endpoint. Use the client's actual unified-snapshot method if the pinned `massive` version provides one; otherwise show the already-correct direct HTTP request only.

### [P2] Do not claim cached prices are re-sent between Massive polls

**File:** `planning/MARKET_INTERFACE.md:164`

The document says the SSE stream re-sends cached prices between Massive polls. The implemented stream emits only when `PriceCache.version` changes, and the Massive source changes the version only after a successful poll. It therefore sends nothing during the 15-second interval or while polling fails. Any frontend behavior designed around a 500 ms server cadence, including a freshness indicator or progressively populated chart, will not occur with the real-data source. Either change the SSE implementation to emit on its timer or correct the contract and make the frontend tolerate the slower event cadence.

## Validation

- Reviewed every untracked file relative to `HEAD` (`8425f7a`). There are no tracked-file modifications.
- `claude plugin validate ./independent-reviewer` passes with an author-metadata warning.
- `claude plugin validate ./.claude-plugin/marketplace.json` passes with marketplace-description and author-metadata warnings.
- Cross-checked the Massive reference against the current official API documentation and the pinned implementation behavior.
