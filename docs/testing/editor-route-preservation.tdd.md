# Course-editor route preservation — TDD evidence

## Required behavior

Drawing is additive unless the user explicitly erased an original span. Every
original, unerased edge remains in the saved route. A drawn stroke connects only
at a real geometric intersection that is also a routing-graph junction; a nearby
pass or grade-separated crossing does not count. Separate strokes are never
flattened into an invented connector.

## RED / GREEN report

The focused regression set initially produced **8 failures and 1 pass**. The
failures covered replacement of the original route, proximity-based false
connections, stroke flattening, and premature save behavior.

After the implementation:

- `.venv/bin/pytest -q tests/test_course_edit.py tests/test_course_pages.py`
  produced **109 passed**.
- `NODE_PATH=/opt/homebrew/lib/node_modules node tests/browser/run_scenarios.js /tmp/runart-harness`
  produced **59 passed**.
- `NODE_PATH=/opt/homebrew/lib/node_modules node tests/browser/mobile_gestures.js /tmp/runart-harness`
  passed at both **390px and 320px** widths.

The environment does not contain the Python `coverage` module, so no percentage
is claimed. No dependency was installed only to manufacture a coverage number.
