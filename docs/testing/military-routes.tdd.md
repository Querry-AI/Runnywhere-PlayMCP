# Military-route runtime gate — TDD evidence

## Source and user journey

The journey came from the reported Samgakji dog course: a runner must never be
shown, download, edit, or restore a route that uses an edge marked `military`.
Existing saved links and build-time presets are subject to the current runtime
rule even when their graph fingerprint still matches.

## RED / GREEN report

| Guarantee | Test | Evidence |
|---|---|---|
| Animal and standard routing reject a military edge | `tests/test_military_routes.py::test_military_edges_are_not_runnable_even_on_pedestrian_roads`, `::test_both_animal_routing_weights_avoid_the_military_shortcut` | RED: runnable weights were returned; GREEN: blocked and alternate path selected |
| Raw edited/saved paths reject military access | `::test_exact_saved_paths_cannot_reintroduce_military_edges` | RED: unsafe path built a `Course`; GREEN: clear `CourseError` |
| Direct, nearby and atlas preset reads share one load-time audit | `::test_matching_graph_fingerprint_does_not_bypass_current_runtime_rules`, `::test_preset_safety_scan_runs_only_once_per_load` | RED: unsafe preset returned; GREEN: unsafe slot omitted and two entries scanned once |
| Cold and warm links/cache cannot restore unsafe data | `::test_blocked_cold_preset_link_does_not_silently_regenerate`, `::test_warm_cache_cannot_bypass_updated_military_rules`, `::test_unsafe_course_cannot_enter_server_cache` | RED: unsafe course returned; GREEN: blocked with no arbitrary regeneration |
| Existing preview, GPX, run, editor, card and thumbnail links explain the block | `::test_existing_unsafe_links_return_a_clear_block_instead_of_route` | RED: HTTP 200; GREEN: HTTP 403 with Korean access explanation |
| Atlas route JSON remains restricted to verified presets and clearly blocks a retired preset | `::test_route_json_stays_preset_only_for_a_safe_hot_generated_course`, `::test_blocked_verified_preset_route_json_returns_clear_403` | RED: a hot generated course leaked through; GREEN: nonpreset is 404 and blocked preset is 403 |
| The bundled Samgakji dog preset is blocked without rebuilding data | `::test_bundled_samgakji_dog_is_not_available_after_runtime_validation` | RED: preset remained available; GREEN: its military edge is detected and preset unavailable |

Initial focused RED command: `.venv/bin/pytest -q tests/test_military_routes.py`
produced **19 failures** caused by the intended missing runtime rule. Focused
GREEN command: `.venv/bin/pytest -q tests/test_military_routes.py` produced
**21 passed**; adding `tests/test_animal_presets.py` produced **27 passed**.

## Coverage and gaps

The environment does not contain the Python `coverage` module, so no percentage
is claimed and no dependency was installed solely for reporting. Synthetic graph
tests cover each boundary and a real-data regression covers the affected bundled
Samgakji preset. No data file was regenerated. Physical navigation through a
military site is deliberately not an automated test.

No TDD checkpoint commits were created because this coordinated task explicitly
forbids commits; this file preserves the RED/GREEN evidence for a later squash or
review.
