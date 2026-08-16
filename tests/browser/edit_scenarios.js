/**
 * Behavioural scenarios for the course detail editing UI.
 *
 * Run against a page built by tests/browser/build_harness.py. Returns a promise
 * resolving to {pass, fail, results} -- `fail` must be 0.
 *
 * These assert what pytest cannot: that feedback is actually *visible*, that a
 * second tap during an in-flight request sends nothing, and that a failed edit
 * leaves the user's work intact.
 */
async function runEditScenarios() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const $ = (id) => document.getElementById(id);

  const overlay = $('editOverlay');
  overlay.setPointerCapture = () => {};
  overlay.releasePointerCapture = () => {};

  const toast = $('editToast');
  const toastText = $('editToastText');
  const toastAction = $('editToastAction');
  const dist = $('editDistance');
  const [draw, erase, undo, cancel, save] =
    ['drawTool', 'eraseTool', 'editUndo', 'editCancel', 'editSave'].map($);

  // visible == rendered with real box area, not merely present in the DOM
  const visible = (el) => {
    if (!el || el.hidden) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };

  const path = initialEditPath;
  const project = ([, lat, lon]) => ({ x: (lon - 126.97) * 1e5, y: (37.57 - lat) * 1e5 });
  const gesture = (i, j) => {
    const rect = overlay.getBoundingClientRect();
    const a = project(path[i]);
    const b = project(path[j]);
    const fire = (type, p) => overlay.dispatchEvent(new PointerEvent(type, {
      pointerId: 1, bubbles: true, clientX: rect.left + p.x, clientY: rect.top + p.y,
    }));
    fire('pointerdown', a);
    for (let k = 1; k <= 6; k++) {
      fire('pointermove', { x: a.x + (b.x - a.x) * k / 6, y: a.y + (b.y - a.y) * k / 6 });
    }
    fire('pointerup', b);
  };

  let calls = [];
  const realFetch = window.fetch;
  const mock = (impl) => {
    window.fetch = (url, opts) => {
      // Pass through anything that is not an edit POST, so a mock left behind
      // by an interrupted run cannot break the next one.
      if (!opts || !opts.body) return realFetch(url, opts);
      calls.push(JSON.parse(opts.body));
      return impl();
    };
  };
  // A successful save calls location.assign(preview_url), which would end the
  // run. window.location.assign cannot be redefined, so the save scenario
  // returns a fragment URL: the same code path runs, but the browser only
  // updates the hash instead of leaving the page.
  const SAVED_URL = '#saved-course';
  const ok = (body) => () => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  const failWith = (status, body) => () =>
    Promise.resolve({ ok: false, status, json: () => Promise.resolve(body) });
  const slow = (body, ms) => () =>
    new Promise((r) => setTimeout(() => r({ ok: true, json: () => Promise.resolve(body) }), ms));

  const results = {};
  const check = (name, actual, expected) => {
    const passed = JSON.stringify(actual) === JSON.stringify(expected);
    results[name] = passed ? 'PASS' : `FAIL (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`;
  };
  // every scenario starts from a known state
  const reset = async () => {
    cancel.click(); await sleep(20);
    $('editRoute').click(); await sleep(20);
    calls = [];
  };

  $('editRoute').click(); await sleep(30);
  check('entry: distance chip visible', visible(dist), true);
  check('entry: distance seeded', dist.textContent, initialLengthKm.toFixed(2) + 'km');

  // T-1  snap failure -> visible error, dismissible, path untouched
  await reset();
  const before = path.length;
  mock(failWith(422, { error: '그린 선이 보행로에서 너무 멀어요.' }));
  draw.click(); gesture(5, 40); await sleep(80);
  check('T-1 error toast visible', visible(toast), true);
  check('T-1 tone', toast.dataset.tone, 'error');
  check('T-1 shows server message', toastText.textContent, '그린 선이 보행로에서 너무 멀어요.');
  check('T-1 dismiss button visible', visible(toastAction), true);
  check('T-1 one request', calls.length, 1);
  await sleep(1200);
  check('T-1 error does not auto-dismiss', visible(toast), true);
  toastAction.click(); await sleep(20);
  check('T-1 dismiss works', visible(toast), false);

  // T-2  save rejected (course_id too long) -> work preserved, save usable again
  await reset();
  mock(failWith(422, { error: '수정한 코스 선이 너무 복잡해 링크로 저장할 수 없어요.' }));
  save.click(); await sleep(80);
  check('T-2 error visible', visible(toast), true);
  check('T-2 message shown', toastText.textContent, '수정한 코스 선이 너무 복잡해 링크로 저장할 수 없어요.');
  check('T-2 save re-enabled', save.disabled, false);

  // T-3  snap success -> distance badge tracks the response
  await reset();
  mock(ok({ path: path.slice(0, 60).concat(path.slice(70)), length_km: 4.37 }));
  draw.click(); gesture(5, 40); await sleep(80);
  check('T-3 tone', toast.dataset.tone, 'success');
  check('T-3 distance badge', dist.textContent, '4.37km');

  // T-4  duplicate requests blocked while one is in flight
  await reset();
  mock(slow({ path, length_km: 5.0 }, 250));
  draw.click(); gesture(5, 40); await sleep(40);
  check('T-4 busy tone', toast.dataset.tone, 'busy');
  check('T-4 tools disabled', [draw, erase, undo, cancel, save].every((b) => b.disabled), true);
  save.click(); gesture(5, 40);            // second taps during the flight
  await sleep(320);
  check('T-4 still one request', calls.length, 1);
  check('T-4 tools re-enabled', draw.disabled, false);

  // T-5  erased gap blocks save, and says why, and keeps saying why
  await reset();
  erase.click(); gesture(20, 55); await sleep(60);
  check('T-5 tone', toast.dataset.tone, 'blocked');
  check('T-5 reason visible', visible(toast), true);
  check('T-5 save disabled', save.disabled, true);
  await sleep(1500);
  check('T-5 reason does not auto-dismiss', visible(toast), true);
  calls = []; save.click(); await sleep(40);
  check('T-5 blocked save sends nothing', calls.length, 0);

  // T-7  undo restores path *and* distance
  await reset();
  mock(ok({ path: path.slice(0, 60).concat(path.slice(70)), length_km: 4.37 }));
  draw.click(); gesture(5, 40); await sleep(80);
  check('T-7 distance after snap', dist.textContent, '4.37km');
  undo.click(); await sleep(30);
  check('T-7 distance after undo', dist.textContent, initialLengthKm.toFixed(2) + 'km');

  // T-8  transient hints auto-dismiss so they do not sit over the map
  await reset();
  draw.click(); await sleep(30);
  check('T-8 hint visible', visible(toast), true);
  await sleep(4000);
  check('T-8 hint auto-dismissed', visible(toast), false);

  // T-9  every tool has a 44px tap target while staying a 40px button
  await reset();
  for (const [name, btn] of [['draw', draw], ['erase', erase], ['undo', undo],
                             ['cancel', cancel], ['save', save]]) {
    const r = btn.getBoundingClientRect();
    check(`T-9 ${name} visual size`, [Math.round(r.width), Math.round(r.height)], [40, 40]);
    // the transparent ::before must own the corners of a 44px box
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const corner = document.elementFromPoint(cx + 21, cy);
    check(`T-9 ${name} hit area reaches 44px`, corner === btn || btn.contains(corner), true);
  }
  // hit areas must not steal each other's taps
  const undoBox = undo.getBoundingClientRect();
  const cancelBox = cancel.getBoundingClientRect();
  check('T-9 neighbours do not overlap', Math.round(cancelBox.left - undoBox.right) >= 4, true);
  check('T-9 save separated from revert', Math.round(save.getBoundingClientRect().left - cancelBox.right), 14);

  // T-10  reverting is undoable, and restores path + distance together
  await reset();
  mock(ok({ path: path.slice(0, 60).concat(path.slice(70)), length_km: 4.37 }));
  draw.click(); gesture(5, 40); await sleep(80);
  check('T-10 edited distance', dist.textContent, '4.37km');
  cancel.click(); await sleep(30);
  check('T-10 reverted distance', dist.textContent, initialLengthKm.toFixed(2) + 'km');
  check('T-10 undo offer visible', visible(toast), true);
  check('T-10 undo offer label', toastAction.textContent, '실행 취소');
  toastAction.click(); await sleep(30);
  check('T-10 edit restored', dist.textContent, '4.37km');
  check('T-10 back in edit mode', document.body.classList.contains('editing'), true);

  // reverting with nothing to discard should not nag
  await reset();
  cancel.click(); await sleep(30);
  check('T-10 no offer when nothing edited', visible(toast), false);

  // T-11  the zoom control cannot move the ground mid-gesture
  await reset();
  check('T-11 zoom control hidden while editing', window.__map.controls.length, 0);
  cancel.click(); await sleep(30);
  check('T-11 zoom control restored on exit', window.__map.controls.length, 1);

  // T-12  a successful save posts action:"save" and navigates to the returned URL
  await reset();
  location.hash = '';
  mock(ok({ course_id: 'NEWID', preview_url: SAVED_URL,
            length_km: 4.9, ascent_m: 20, rfs: 60 }));
  save.click(); await sleep(80);
  check('T-12 navigates to returned url', location.hash, SAVED_URL);
  check('T-12 posts a save action', calls.length && calls[calls.length - 1].action, 'save');
  check('T-12 stays locked while navigating', save.disabled, true);
  location.hash = '';

  window.fetch = realFetch;
  const values = Object.values(results);
  return {
    pass: values.filter((v) => v === 'PASS').length,
    fail: values.filter((v) => v !== 'PASS').length,
    results,
  };
}

if (typeof module !== 'undefined') module.exports = { runEditScenarios };
