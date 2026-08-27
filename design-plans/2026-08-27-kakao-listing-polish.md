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

- Basic root: vertical layout, zero gap/inset, no Card component. See the live verification below for why Card's border override was insufficient.
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
