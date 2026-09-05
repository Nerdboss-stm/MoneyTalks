# Gate

Rule: an answer containing a figure absent from the evidence JSON, or with driver coverage below 50%, fails.

Evaluator of record: `backend/explain/verify.py` (`check(text, evidence)`), run on every answer before it reaches `voice.speak` or the screen in v2, and after the answer in v1 for scoring. Its output travels with each exchange to PRISM as trace metadata `verifier` and as the `verify` tool step of the run's trajectory.

PRISM Evaluators Hub: per `docs/prism-notes.md` the Evaluators Hub is a Builder-plan capability and this project is on Free (the Hub is locked in the dashboard). The rule above could not be expressed there within the ten-minute cap, so verify.py is the evaluator of record. PRISM recommends. I apply.
