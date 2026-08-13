# Codex Project Instructions

## Bug Fix Policy

When fixing bugs in this repository, always follow the principle:

> Fix the bug, not the surrounding code.
> Minimum necessary change.

### Scope control

- Only modify code directly related to the current task.
- Do not modify unrelated code.
- Do not perform opportunistic refactoring.
- Do not rename unrelated variables, functions, files, or directories.
- Do not reformat entire files.
- Do not upgrade dependencies unless explicitly requested.
- Do not modify public APIs unless required by the bug itself.
- Do not clean up legacy code unless explicitly requested.

If a bug can be fixed with a small local change, prefer that over a broader architectural change.

### Before editing

Before making changes:

1. Identify the root cause.
2. Identify the smallest code path responsible for the issue.
3. Determine which files actually need modification.
4. Consider whether the proposed change can affect other call paths.

Do not make speculative changes when the root cause is unclear.

### During implementation

Preserve existing behavior unless changing that behavior is required by the task.

In particular, preserve:

- public APIs
- function signatures
- return values
- data structures
- configuration formats
- error-handling semantics
- logging semantics
- compatibility with existing callers

Prefer local fixes over changes to shared/common code.

### Unrelated issues

If you discover another bug or design problem while working:

- Do not fix it.
- Report it separately.
- Continue only with the requested task.

### Diff review

After implementation, review the complete git diff.

Verify:

1. Every changed line is necessary for the requested fix.
2. No unrelated cleanup or formatting changes were introduced.
3. No unnecessary files were modified.
4. Existing behavior outside the target scenario remains unchanged.
5. The change cannot be reduced further without compromising correctness.

If unrelated changes exist, revert them.

### Validation

Run the smallest relevant validation first:

- targeted unit tests
- regression tests for the bug
- related integration tests
- lint/type checks if relevant

Verify both:

1. The original bug is fixed.
2. Existing normal behavior still works.

Do not modify unrelated production code merely to make tests pass.

### Test failures

When an existing test fails after the change:

First determine whether:

- the implementation introduced a regression, or
- the requested behavior intentionally changed.

Do not modify tests merely to hide a regression.

### Escalation rule

If fixing the task appears to require modifying unrelated modules or substantially expanding the scope:

Do not silently expand the change.

Explain:

- why the additional change appears necessary;
- what modules would be affected;
- what risks it introduces;
- whether a smaller alternative exists.

### Final requirement

Prefer the solution with:

1. the fewest modified files;
2. the smallest reasonable diff;
3. the smallest behavioral impact;
4. the greatest backward compatibility.

Any code unrelated to the current task must not be modified, even if it appears incorrect or could be improved.