# Copilot Instructions

- Follow the PEP 8 style guide and the formatting conventions already established in the repository.
- Use type hints for every function signature, including parameters and return values. Use precise types where practical instead of `Any`.
- Write concise docstrings for all public functions, classes, and modules. Document important parameters, return values, and raised exceptions when they are not obvious.
- Use 4 spaces for indentation; never use tabs for Python indentation.
- Keep changes focused and preserve existing public APIs unless the task requires an API change.
- Add comments before genuinely non-trivial code fragments when they explain intent, an invariant, or a non-obvious trade-off. Do not add comments that merely restate the code.
- After making edits, run `make build` to run the repository's audit, synchronization, pre-commit, test, and coverage checks. Report any failures and their cause.
- Add or update focused tests for behavior changes, and keep the test suite passing.
