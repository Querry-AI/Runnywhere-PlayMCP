# Share button runtime fix — TDD evidence

## Source and journey

No plan file was used. The journey came from the reproduced KC production
failure: as a runner, I can press **공유하기** on the run page and copy the
current course link without a JavaScript error.

## Task report

- Production reproduction: clicking the KC button emitted
  `ReferenceError: currentCourseUrl is not defined`; the button text did not
  change.
- RED: `.venv/bin/python -m pytest tests/test_course_detail.py -q -k sharing_an_edited_course`
  failed because `currentCourseUrl` was declared after the outer click handler.
- GREEN: the same command passed after moving the declaration into the shared
  script scope.
- Related regression suite:
  `.venv/bin/python -m pytest tests/test_course_detail.py tests/test_course_pages.py -q`
  — `51 passed`.
- Full suite: `.venv/bin/python -m pytest -q` — `798 passed`.
- Browser verification: a local run page changed the button label to
  `링크가 복사됐어요!` after clicking and produced no browser console errors.

## Test specification

| What is guaranteed | Test | Type | Result |
|---|---|---|---|
| The current-course URL binding exists before the share click handler is registered | `tests/test_course_detail.py::test_sharing_an_edited_course_sends_the_edited_link` | Regression | PASS |
| Course detail and run-page rendering remain valid | `tests/test_course_detail.py tests/test_course_pages.py` | Integration | PASS (51) |
| The repository test suite has no regression | `pytest -q` | Full suite | PASS (798) |
| Clicking Share copies the link and does not throw | Local browser run-page check | Browser | PASS |

## Coverage and known gaps

The environment does not include `pytest-cov` or the `coverage` module, so a
numeric coverage report was unavailable. No dependency was installed solely
for this fix. The focused regression, related integration tests, full suite,
and real browser click path all passed.

## Merge evidence

- RED checkpoint: `0b10970 test(ui): reproduce inert share button`
- GREEN checkpoint: `cf1ee9c fix(ui): restore course sharing action`
