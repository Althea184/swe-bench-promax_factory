# Problem statement generation policy

Source: TaskSpec only. Do not pass a full gold diff to the generator. Target medium abstraction and medium specificity. Control the information disclosed, not merely the word count.

Include the refactoring goal, affected functionality, behavior to preserve, and compatibility requirements when relevant. Name a public interface only when it is essential to identify the requested change.

Avoid exact edit steps, line numbers, unnecessary file paths, exhaustive call-site lists, patch reconstruction, and implementation details the solver should discover.

Too vague: "Improve the architecture of the project."

Too detailed: "Rename render_amount in money.py to format_amount, then update invoice.py and quote.py."

Target scale: "Refactor the amount-formatting API to use clearer naming and migrate dependent callers while preserving existing formatting behavior."

This is a reference template for future model-backed generation. The current generator is deterministic and records its own quality checks.
