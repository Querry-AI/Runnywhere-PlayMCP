# Kakao course listing polish — 2026-08-27

## Reference and scope

Primary reference: the user-supplied Kakao travel listing screenshot.
Preserve the existing image-left / information-right layout and section headings.
Within the information column: tags, course title, then metrics beside the action.
Course names, traits, facilities, distance, time, ascent and target URLs remain
derived from each course; the Bongcheon example is not hardcoded into the builder.

Mobbin MCP screens inspected:

- [Viator](https://mobbin.com/screens/b4d59aa2-e252-441d-acbd-47d3259bbeb0): compact category pill, title and subordinate details, right-aligned price. Its card shadows are not adopted.
- [Booking.com](https://mobbin.com/screens/e8b417ce-4f0a-4bfe-887c-47eb4ec9b208): compact image/text hierarchy and muted secondary details. Its separators are not adopted.
- [Agoda](https://mobbin.com/screens/fbc0ed79-5afa-4121-b2c6-3bc505b00b8e): unboxed image/text row and secondary metadata. Its promotional sections are not adopted.

## Widget changes

- Card root: restored after the Basic compatibility regression documented below;
  12px horizontal inset, transparent background/border override.
- Thumbnail: retain 88 × 88, unframed, medium corner radius; image/text gap 8px.
- Tags: soft blue `info` badges, small and pill-shaped, 4px gaps, whole-tag wrapping.
- Course title: small semibold Text (14px), one line, dark-mode #f5f5f5 / light-mode #202020.
- Distance/time: small bold Text with the same foreground; ascent: large Caption, dark-mode #a6a6a6 / light-mode #666666.
- Information gap 6px, metric gap 4px, metrics/action gap 8px; row top/bottom padding 12px/16px.
- Action: small solid primary pill labelled 지도 보기, unchanged per-course URL.

These are supported ChatKit properties, not CSS injected into Kakao:
[Card](https://openai.github.io/chatkit-js/api/openai/chatkit/namespaces/widgets/type-aliases/card/),
[Badge](https://openai.github.io/chatkit-js/api/openai/chatkit/namespaces/widgets/type-aliases/badge/),
[Button](https://openai.github.io/chatkit-js/api/openai/chatkit/namespaces/widgets/type-aliases/button/).
Text/Title/Badge sizes and the primary button palette are renderer tokens;
their exact pixel values and any outer host decoration must be checked in Kakao.

## Verification boundary

The widget tests cover the requested Bongcheon copy, data-driven alternatives,
borderless envelope, tag wrapping, metrics/action ordering and per-course links.
No replacement HTML renderer is used as evidence of Kakao parity.
## Live verification after ff9c9fd deployment

Sent the user-approved Bongcheon cat-course request in the signed-in Kakao Preview.
The actual response contains the requested four tags, `🐱 봉천역 야옹런`,
`7.5km · 약 53분`, `오르막 106m`, and three per-course 지도 보기 buttons.
Health endpoint returned ok/ready, but release_sha is unknown; the new badges
in the actual response establish that the styling changes were deployed.

Read-only computed-style measurements in the actual chat iframe:

- Card ancestor: **1px solid border**, despite border `{size: 0, color: "transparent"}`; no inline border override was emitted. Padding/background overrides do work.
- Badge sm: 12px / 18px, blue soft pill, horizontal padding 6.65px.
- Title sm: 18px / 26px, weight 600; too large for the reference's compact title.
- Caption md: 12px / 15.6px.
- Map button sm: 14px, weight 500, pill radius 9999px, horizontal padding 13.3px; the current light host uses white text on a dark fill.
- Clicking 지도 보기 produced no captured error, but no navigated tab was exposed by the browser runtime. Destination opening is not claimed verified.

Follow-up change: replace Card with the documented
[Basic root](https://openai.github.io/chatkit-js/api/openai/chatkit/namespaces/widgets/type-aliases/basicroot/),
preserving all listing children, and use Text sm for 14px course titles.
Increase ascent to Caption lg. This new root must still be verified after
deployment in Kakao; documentation support is not evidence of host parity.

## Spacing and unavailable-course follow-up

- Basic root now has 12px horizontal padding: section headings and thumbnails
  share the same left inset, while map buttons no longer touch the right edge.
- Course row vertical padding reduced from 12px/16px to 8px/8px; metric/action
  gap increased from 8px to 12px.
- Non-exact plans place their explanation in a Markdown sibling before
  `추천 코스`, inside the unframed envelope. This preserves Kakao's requirement
  that content[0] is widget JSON while fixing the visible order independently
  of whether the assistant follows the before_widget metadata.
- `assistant_text_in_widget` tells the assistant not to repeat that paragraph.
  Exact matches retain the existing standalone assistant-text contract.
- Missing local animal and distance/time mismatch use different explanations;
  a known local animal with the wrong length is not described as nonexistent.
- The share copy also starts with the same explanation. Candidate ranking,
  route generation and per-course map URLs are unchanged.

Verification note: the existing pool-unavailable animal-generation test returns
no route in the current environment. The same failure was reproduced with
pre-change HEAD versions of server.py, widget.py and courseplan.py in an isolated
temporary package using the same data. No generation budget or route logic was
changed to hide that unrelated failure. An initial focused run also exposed the
existing hardcoded 73-minute nearest-route expectation returning 85 minutes;
the subsequent full run passed that test.

## Widget disappearance and contradictory animal copy — correction

The upstream Basic-root experiment above is superseded. On 2026-08-27 the
deployed server returned Basic for 서울대입구역/dog, with a real 9.3km dog
course in its share copy. A fresh signed-in Preview request for
`서울역에서 강아지 모양 러닝 코스 그려줘` displayed only prose and no widget.
The previously deployed Card response in the same Preview did render.

- Restore the known-rendering Card envelope, keeping compact rows, heading
  weight/color and spacing. Do not claim the host's outer border is removed:
  it previously ignored the zero-border override, and upstream support alone
  does not prove Kakao support.
- Send `course_selection` with requested type separate from actual primary
  and alternative course facts, exact map URLs and shape-match booleans.
- Derive `assistant_final_text` from the same course as the widget. It names
  the actual shape and supplies a direct link; it never claims that a card
  rendered or a requested animal was created when a different shape won.
- If widget serialization fails after ranking, render the selected plan as
  Markdown with all its links. Never revert to the original generator's copy
  for another animal. Cached single-course responses use the same facts.
- Plain “러닝 코스 그려줘” explicitly means standard unless animal art was
  actually requested. Existing start/effort/shape ranking is unchanged.

Regression tests cover dog→rabbit, dog→standard, exact dog, widget failure,
actual alternatives, cached responses and standard dispatch. This proves the
server contract only; the repaired payload and assistant wording still need
an actual Kakao deployment and fresh-conversation check.

Validation: 431 tests passed with the previously documented pool-unavailable
generation test excluded. That test was rerun alone and still failed because
no whale course returned within its existing response budget (4.24s test run).
No generation budgets were changed.

## Three-course bundles, heading inset, and mandatory night lighting

- A successful recommendation now contains exactly three distinct courses in
  one MCP result. Standard requests generate alternative search bearings
  internally within the existing response budget. IDs preserve the selected
  variant; the legacy zero-variant ID format is unchanged. Identical road-edge
  sets, including reverse traversals, cannot fill multiple slots.
- Tool instructions and descriptions explicitly require one call per request,
  not one call per course. `course_selection` records requested/returned counts,
  and final assistant copy describes all three actual courses. An incomplete
  bundle returns `insufficient_courses`, without a singleton widget or an
  instruction to make additional calls. This is a server contract and model
  instruction, not proof of the hosted assistant's call count.
- Add 12px top padding to the existing Card, preserving its 12px horizontal
  inset, heading styles, and compact listing structure.
- Night lighting is mandatory, not a preference that may be relaxed during
  fallback. The shared threshold uses the existing good-lighting band (0.60);
  missing/unknown measurements do not qualify. Apply it to generated routes,
  every ranked candidate, and cached/refined courses returned as night runs.
- A request flag alone no longer creates a safety badge. Qualifying courses
  say `야간 조명 양호`; unqualified historical detail pages retain a warning.
  Fewer than three verified candidates yields an explicit shortage response,
  never dark courses disguised as safe ones. Lighting data is not a guarantee
  of real-world safety or a reason to hide other route caveats.
- Tests cover real distinct-route generation and restorable IDs, controlled
  lighting measurements at the public MCP boundary, missing/poor-lighting
  rejection, night-preserving fallbacks, cached refinement, and widget padding.
  Controlled measurements are isolated fixtures, not production data changes.
- Stabilize the existing duration-refusal copy test with a fixed router error:
  its purpose is minute conversion, not asserting which loop a timed search
  discovers under machine load. The separate pool-unavailable whale timeout
  regression remains unresolved; route time limits were not increased.

Deployment note: the last direct check of the running Kakao endpoint still
returned the superseded Basic root although origin/main contained the Card
repair. These changes require a successful redeployment of this revision and
a fresh Preview conversation before live widget rendering/call count can be
claimed verified.

Validation for this revision: 443 tests passed with the known pool-unavailable
whale-generation test excluded (86.51s). A full run reproduced that existing
failure before exclusion; it still returns no route within the original
budget. The lighting/Markdown-focused tests also passed (41 tests).
