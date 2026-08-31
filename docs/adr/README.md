# Architecture Decision Records

Short records of decisions that were hard to make and are expensive to reverse.
Each says what we chose, what we gave up, and what would make us change our mind.

New ADRs are numbered sequentially and never edited after acceptance — a
superseded ADR gets a `Superseded by` line and stays put, because the reasoning
that was wrong is as useful as the reasoning that was right.

| # | Decision | Status |
|---|---|---|
| [0001](0001-capabilities-over-exceptions.md) | Engines declare capabilities instead of raising | Accepted |
| [0002](0002-truthful-outcomes.md) | Every action reports whether anything changed | Accepted |
| [0003](0003-narrow-engine-interface.md) | A narrow action-level engine interface, not a Page object | Accepted |
| [0004](0004-async-engine-interface.md) | The engine interface is async | Accepted |
