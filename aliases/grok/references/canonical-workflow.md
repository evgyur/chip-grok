# Canonical workflow

This compatibility command contains no independent runtime logic. It exists only so Hermes can discover `/grok` and load the sibling `chip-grok` skill.

If `chip-grok` is missing or cannot be loaded, stop with a blocker. Do not fall back to a model switch, raw Grok execution in the source checkout, or an unverified direct edit.
