/**
 * Behavioural scenarios for the course detail pages, run in a real browser.
 *
 * pytest can only assert that a string is present in the rendered page. It
 * cannot tell "the page says X" from "the page says X where nobody can read
 * it" -- the sr-only edit bar passed every string assertion for two commits
 * while showing users nothing (F-01).
 *
 * This drives the *production* page script against the Kakao double from
 * build_harness.py, so real DOM state, real CSS visibility and real pointer
 * gestures are observed.
 *
 *     .venv/bin/python tests/browser/build_harness.py /tmp/runart-harness
 *     NODE_PATH=$(npm root -g) node tests/browser/run_scenarios.js /tmp/runart-harness
 *
 * Exits non-zero if any scenario fails.
 */
const { chromium } = require('playwright');
const path = require('path');

const DIR = process.argv[2] || path.join(__dirname, 'harness');

async function page(browser, file) {
  const p = await browser.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(String(e)));
  p.on('console', m => { if (m.type() === 'error' && !/ERR_FILE_NOT_FOUND/.test(m.text())) errors.push('console: ' + m.text()); });
  await p.setViewportSize({ width: 390, height: 844 });
  await p.goto('file://' + path.join(DIR, file));
  await p.waitForTimeout(250);
  return { p, errors };
}

const SUMMARY = km => ({
  course_id: 'abc', length_km: km, ascent_m: 40, elev_range: [10, 30],
  grade_label: '평지 위주', duration_min: [30, 40], signals: 5,
  facility_counts: { convenience_store: 3, restroom: 1 }, facility_rows: [],
  traits: [{ emoji: '🌳', label: '녹지' }], highlights: ['좋아요'], cautions: [],
  start_name: '서울시청', title: km.toFixed(1) + 'km 서울시청런',
  name_placeholder: '서울시청런',
  badges: [{ emoji: '🏟️', label: '일반 러닝 코스', detail: '설명' }],
});

const results = [];
const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });

(async () => {
  const browser = await chromium.launch();

  // ---- 1. every page loads its script to the end (no ReferenceError) ----
  for (const file of ['harness.html', 'harness_info.html', 'harness_info_animal.html', 'harness_animal.html']) {
    const { p, errors } = await page(browser, file);
    const reached = await p.evaluate(() => typeof window.__map === 'object' && !!window.__map);
    check(`${file}: script runs to the end`, errors.length === 0 && reached, errors.join(' | '));
    await p.close();
  }

  // ---- 2. badge tooltips ----
  {
    const { p, errors } = await page(browser, 'harness_info.html');
    const tipCount = await p.locator('.badge-wrap .badge-tip').count();
    check('info: every badge has a tooltip', tipCount >= 2, `tips=${tipCount}`);
    const hiddenFirst = await p.locator('.badge-tip').first().evaluate(
      el => getComputedStyle(el).visibility);
    await p.locator('.badge-wrap .badge').first().click();
    await p.waitForTimeout(120);
    const shown = await p.locator('.badge-tip').first().evaluate(
      el => getComputedStyle(el).visibility);
    const expanded = await p.locator('.badge-wrap .badge').first().getAttribute('aria-expanded');
    check('badge tooltip opens on tap', hiddenFirst === 'hidden' && shown === 'visible' && expanded === 'true',
      `${hiddenFirst} -> ${shown}, expanded=${expanded}`);
    const text = await p.locator('.badge-tip').first().innerText();
    check('badge tooltip explains the badge', text.length > 12, JSON.stringify(text));
    await p.locator('body').click({ position: { x: 5, y: 5 } });
    await p.waitForTimeout(120);
    const stillOpen = await p.locator('.badge-wrap').first().evaluate(el => el.classList.contains('open'));
    check('badge tooltip closes on outside tap', stillOpen === false, `open=${stillOpen}`);
    check('no page errors while using tooltips', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  // ---- 3. the map stays draggable on the info page ----
  {
    const { p } = await page(browser, 'harness_info.html');
    const draggable = await p.evaluate(() => window.__map.draggable);
    check('info: map is draggable', draggable === true, `draggable=${draggable}`);
    // a drag that starts on a facility marker must move the map immediately
    const moved = await p.evaluate(() => {
      const marker = document.querySelector('.facility-marker');
      if (!marker) return 'no marker';
      marker.setPointerCapture = () => {};
      const before = window.__map.centerCount || 0;
      const fire = (type, x, y) => marker.dispatchEvent(new PointerEvent(type, {
        pointerId: 7, bubbles: true, clientX: x, clientY: y }));
      fire('pointerdown', 100, 100);
      fire('pointermove', 140, 130);
      fire('pointermove', 180, 160);
      fire('pointerup', 180, 160);
      return (window.__map.centerCount || 0) - before;
    });
    check('info: dragging from a facility marker pans the map', moved >= 2,
      `setCenter calls=${moved}`);
    const panCount = await p.evaluate(() => window.__map.panCount || 0);
    check('info: no animated panBy during a drag', panCount === 0, `panBy=${panCount}`);
    await p.close();
  }

  // ---- 4. the animal course opens on its silhouette ----
  {
    const { p } = await page(browser, 'harness_info_animal.html');
    const cls = await p.evaluate(() => document.body.className);
    check('animal info page opens on the silhouette', cls.includes('shape-only'), cls);
    const active = await p.locator('#shapeView').getAttribute('class');
    check('silhouette toggle reads as selected', (active || '').includes('active'), active);
    await p.close();
  }
  {
    const { p } = await page(browser, 'harness_info.html');
    const cls = await p.evaluate(() => document.body.className);
    check('plain info page still opens on the running guide', !cls.includes('shape-only'), cls);
    await p.close();
  }

  // ---- 5. the editor draws the real street geometry ----
  {
    const { p } = await page(browser, 'harness.html');
    const info = await p.evaluate(() => ({
      nodes: initialEditPath.length,
      geom: initialEditGeometry.length,
      shaped: initialEditGeometry.filter(Boolean).length,
      drawn: (window.__lines || []).filter(l => l._map && l._o.strokeColor === '#087b59')
        .map(l => l._o.path.length)[0] || 0,
    }));
    check('editor geometry is shipped per edge', info.geom === info.nodes - 1,
      JSON.stringify(info));
    check('editor line has more points than it has nodes', info.drawn > info.nodes,
      `drawn=${info.drawn} nodes=${info.nodes} shaped edges=${info.shaped}`);
    await p.close();
  }

  // ---- 6. the eraser grows along the line, and erasing reconnects ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async (SUMMARY_JSON) => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const at = i => {
        const s = proj.containerPointFromCoords(
          new kakao.maps.LatLng(initialEditPath[i][1], initialEditPath[i][2]));
        return { x: s.x, y: s.y };
      };
      const fire = (type, pt) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 3, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      document.getElementById('eraserTool').click();
      // sweep four consecutive nodes
      fire('pointerdown', at(6));
      for (const i of [7, 8, 9]) fire('pointermove', at(i));
      fire('pointerup', at(9));
      const swept = document.getElementById('selErase').hidden === false;

      let sent = null;
      window.fetch = async (url, opts) => {
        sent = JSON.parse(opts.body);
        const path = initialEditPath.map(p => p[0]);
        return { ok: true, json: async () => ({
          path: initialEditPath.map(p => [...p]),
          geometry: initialEditGeometry.slice(),
          length_km: 4.44, note: '', summary: SUMMARY_JSON }) };
      };
      document.getElementById('selErase').click();
      await new Promise(r => setTimeout(r, 120));
      return { swept, sent, distance: document.getElementById('editDistance').textContent,
        toast: document.getElementById('editToastText').textContent };
    }, SUMMARY(4.44));
    check('eraser sweep marks a span', out.swept === true, JSON.stringify(out.swept));
    check('erasing asks the server to reconnect the gap',
      out.sent && out.sent.action === 'reroute', JSON.stringify(out.sent && out.sent.action));
    check('erasing applies the reconnected route', out.distance === '4.44km', out.distance + ' | ' + out.toast);
    check('no page errors while erasing', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  // ---- 7. one sweep never jumps to the far side of the loop ----
  {
    const { p } = await page(browser, 'harness.html');
    const span = await p.evaluate(() => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const at = i => {
        const s = proj.containerPointFromCoords(
          new kakao.maps.LatLng(initialEditPath[i][1], initialEditPath[i][2]));
        return { x: s.x, y: s.y };
      };
      const fire = (type, pt) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 4, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      document.getElementById('eraserTool').click();
      fire('pointerdown', at(4));
      for (const i of [5, 6, 7]) fire('pointermove', at(i));
      // now jump the finger to the opposite end of the loop: a real finger
      // cannot do this, and neither should the selection follow it.
      const far = initialEditPath.length - 5;
      fire('pointermove', at(far));
      fire('pointerup', at(far));
      const line = (window.__lines || []).filter(l => l._map && l._o.strokeColor === '#e0522d').pop();
      return { total: initialEditPath.length, selected: line ? line._o.path.length : 0 };
    });
    check('a jump across the loop does not swallow the course',
      span.selected > 0 && span.selected < span.total / 3, JSON.stringify(span));
    await p.close();
  }

  // ---- 8. the drawing tool pulls the line instead of drawing it ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async (SUMMARY_JSON) => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const screen = (lat, lon) => {
        const s = proj.containerPointFromCoords(new kakao.maps.LatLng(lat, lon));
        return { x: s.x, y: s.y };
      };
      const fire = (type, pt) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 5, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      const sent = [];
      window.fetch = async (url, opts) => {
        sent.push(JSON.parse(opts.body));
        return { ok: true, json: async () => ({
          path: initialEditPath.map(q => [...q]),
          geometry: initialEditGeometry.slice(),
          length_km: 5.55, note: '', summary: SUMMARY_JSON }) };
      };
      document.getElementById('drawTool').click();
      const pressed = document.getElementById('drawTool').getAttribute('aria-pressed');
      const draggable = window.__map.draggable;

      // grab the line, pull it away, release
      const grab = screen(initialEditPath[10][1], initialEditPath[10][2]);
      fire('pointerdown', grab);
      const grabbed = sent.length;
      for (const step of [1, 2, 3, 4]) {
        fire('pointermove', { x: grab.x + step * 26, y: grab.y + step * 26 });
        await new Promise(r => setTimeout(r, 30));
      }
      fire('pointerup', { x: grab.x + 104, y: grab.y + 104 });
      await new Promise(r => setTimeout(r, 200));
      return { pressed, draggable, grabbed, sent,
        distance: document.getElementById('editDistance').textContent };
    }, SUMMARY(5.55));

    check('drawing tool engages', out.pressed === 'true', out.pressed);
    check('the map stops panning while the line is being pulled',
      out.draggable === false, `draggable=${out.draggable}`);
    check('grabbing alone changes nothing', out.grabbed === 0, `requests=${out.grabbed}`);
    check('pulling re-routes on the walking graph, not freehand',
      out.sent.length >= 2 && out.sent.every(r => r.action === 'via'),
      JSON.stringify(out.sent.map(r => r.action)));
    check('each pull carries one point, the finger',
      out.sent.every(r => r.vias && r.vias.length === 1),
      JSON.stringify(out.sent.map(r => r.vias && r.vias.length)));
    // A span that grew with the pull let the replacement swallow more route
    // than the detour added, so pulling harder made the course shorter.
    check('the span stays where the finger grabbed it',
      new Set(out.sent.map(r => `${r.from_index}..${r.to_index}`)).size === 1,
      out.sent.map(r => `${r.from_index}..${r.to_index}`).join(' '));
    check('every pull re-routes the original span, never a stacked one',
      new Set(out.sent.map(r => JSON.stringify(r.path))).size === 1,
      `distinct base paths=${new Set(out.sent.map(r => JSON.stringify(r.path))).size}`);
    check('the pulled distance lands on screen', out.distance === '5.55km', out.distance);
    check('no page errors while pulling', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  // ---- 8b. a whole pull is one undo step, not one per sample ----
  {
    const { p } = await page(browser, 'harness.html');
    const undos = await p.evaluate(async (SUMMARY_JSON) => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const s = proj.containerPointFromCoords(
        new kakao.maps.LatLng(initialEditPath[10][1], initialEditPath[10][2]));
      const fire = (type, pt) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 6, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      window.fetch = async () => ({ ok: true, json: async () => ({
        path: initialEditPath.map(q => [...q]),
        geometry: initialEditGeometry.slice(),
        length_km: 6.66, note: '', summary: SUMMARY_JSON }) });
      document.getElementById('drawTool').click();
      fire('pointerdown', { x: s.x, y: s.y });
      for (const step of [1, 2, 3, 4, 5]) {
        fire('pointermove', { x: s.x + step * 30, y: s.y + step * 30 });
        await new Promise(r => setTimeout(r, 30));
      }
      fire('pointerup', { x: s.x + 150, y: s.y + 150 });
      await new Promise(r => setTimeout(r, 250));
      const pulled = document.getElementById('editDistance').textContent;
      document.getElementById('editUndo').click();
      await new Promise(r => setTimeout(r, 80));
      return { pulled, afterOne: document.getElementById('editDistance').textContent,
        original: initialLengthKm.toFixed(2) + 'km',
        undoStillOn: document.getElementById('editUndo').disabled === false };
    }, SUMMARY(6.66));
    check('a pull moves the distance', undos.pulled === '6.66km', undos.pulled);
    check('one undo takes back the whole pull',
      undos.afterOne === undos.original, `${undos.afterOne} vs ${undos.original}`);
    check('the pull left no extra undo steps behind',
      undos.undoStillOn === false, `undo enabled=${undos.undoStillOn}`);
    await p.close();
  }

  // ---- 9. saving asks for a name ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async () => {
      let sent = null;
      window.fetch = async (url, opts) => {
        sent = JSON.parse(opts.body);
        return { ok: true, json: async () => ({ preview_url: '#saved' }) };
      };
      document.getElementById('editSave').click();
      const sheet = document.getElementById('nameSheet');
      const input = document.getElementById('nameSheetInput');
      const openState = { hidden: sheet.hidden, value: input.value, placeholder: input.placeholder,
        grey: getComputedStyle(input).getPropertyValue('color') };
      // save with nothing typed
      document.getElementById('nameSheetSave').click();
      await new Promise(r => setTimeout(r, 120));
      return { openState, sent };
    });
    check('save opens the rename sheet', out.openState.hidden === false, JSON.stringify(out.openState.hidden));
    check('the field starts empty', out.openState.value === '', JSON.stringify(out.openState.value));
    check('the current name sits behind it as a placeholder',
      /런|코스/.test(out.openState.placeholder), JSON.stringify(out.openState.placeholder));
    check('saving untouched keeps the current name',
      out.sent && out.sent.action === 'save' && out.sent.name === '',
      JSON.stringify(out.sent));
    check('no page errors while saving', errors.length === 0, errors.join(' | '));
    await p.close();
  }
  {
    const { p } = await page(browser, 'harness.html');
    const sent = await p.evaluate(async () => {
      let body = null;
      window.fetch = async (url, opts) => {
        body = JSON.parse(opts.body);
        return { ok: true, json: async () => ({ preview_url: '#saved' }) };
      };
      document.getElementById('editSave').click();
      const input = document.getElementById('nameSheetInput');
      input.value = 'AA런';
      document.getElementById('nameSheetSave').click();
      await new Promise(r => setTimeout(r, 120));
      return body;
    });
    check('a typed name is sent with the save', sent && sent.name === 'AA런', JSON.stringify(sent && sent.name));
    await p.close();
  }

  // ---- 10. reverting restores the line, not half of it ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async (SUMMARY_JSON) => {
      const drawnLength = () => {
        const line = (window.__lines || [])
          .filter(l => l._map && l._o.strokeColor === '#087b59').pop();
        return line ? line._o.path.length : 0;
      };
      const before = drawnLength();
      // an edit that returns a *shorter* path with matching geometry
      window.fetch = async () => ({ ok: true, json: async () => ({
        path: initialEditPath.slice(0, 40).concat([initialEditPath[0]]),
        geometry: new Array(40).fill(null),
        length_km: 2.2, note: '', summary: SUMMARY_JSON }) });
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const on = proj.containerPointFromCoords(
        new kakao.maps.LatLng(initialEditPath[10][1], initialEditPath[10][2]));
      const fire = (t, pt) => overlay.dispatchEvent(new PointerEvent(t, {
        pointerId: 9, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      document.getElementById('drawTool').click();
      fire('pointerdown', on); fire('pointerup', on);
      await new Promise(r => setTimeout(r, 150));
      const edited = drawnLength();
      document.getElementById('editCancel').click();
      await new Promise(r => setTimeout(r, 80));
      const reverted = document.getElementById('editDistance').textContent;
      // 실행 취소 of the revert re-enters the editor with the discarded state:
      // nodes and street geometry have to come back together, or the line is
      // drawn from one edit's nodes and another edit's shapes.
      document.getElementById('editToastAction').click();
      await new Promise(r => setTimeout(r, 80));
      return { before, edited, distance: document.getElementById('editDistance').textContent,
        original: initialLengthKm.toFixed(2) + 'km',
        reverted, restored: drawnLength() };
    }, SUMMARY(2.2));
    check('an edit changes the drawn line', out.edited > 0 && out.edited !== out.before,
      JSON.stringify(out));
    check('reverting puts the original distance back',
      out.reverted === out.original, `${out.reverted} vs ${out.original}`);
    check('undoing a revert brings back the edit, nodes and shapes together',
      out.restored === out.edited, `${out.restored} vs ${out.edited} points`);
    check('no page errors while reverting', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  await browser.close();

  const failed = results.filter(r => !r.ok);
  for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  — ' + r.detail : ''}`);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})();
