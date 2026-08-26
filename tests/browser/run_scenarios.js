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
    const { p, errors } = await page(browser, 'harness.html');
    const moved = await p.evaluate(() => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const before = window.__map.centerCount || 0;
      const fire = (type, x, y) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 2, pointerType: 'touch', bubbles: true, clientX: x, clientY: y }));
      fire('pointerdown', 180, 260);
      fire('pointermove', 210, 285);
      fire('pointermove', 245, 310);
      fire('pointerup', 245, 310);
      return {
        centers: (window.__map.centerCount || 0) - before,
        panPressed: document.getElementById('panTool').getAttribute('aria-pressed'),
        toolActive: document.body.classList.contains('tool-active')
      };
    });
    check('edit: one-finger drag pans in map-move mode',
      moved.centers >= 2 && moved.panPressed === 'true' && moved.toolActive === false,
      JSON.stringify(moved));
    check('edit: mobile map drag has no page errors', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  // ---- 6. the editor draws the real street geometry ----
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

  // ---- 6. the eraser grows along the line and leaves a local red gap ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async () => {
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
        return { ok: true, json: async () => ({ preview_url: '#unexpected' }) };
      };
      document.getElementById('selErase').click();
      await new Promise(r => setTimeout(r, 40));
      const red = (window.__lines || []).filter(
        l => l._map && l._o.strokeColor === '#e5322e').pop();
      return { swept, sent,
        redPoints: red ? red._o.path.length : 0, redOpacity: red && red._o.strokeOpacity,
        distance: document.getElementById('editDistance').textContent,
        state: document.getElementById('editDraftState').textContent };
    });
    check('eraser sweep marks a span', out.swept === true, JSON.stringify(out.swept));
    check('erasing makes no route-generation request', out.sent === null, JSON.stringify(out.sent));
    check('the erased geometry remains translucent red',
      out.redPoints > 1 && out.redOpacity === 0.32, JSON.stringify(out));
    check('the route is visibly marked incomplete',
      /미완성/.test(out.distance) && out.state === '연결 필요', `${out.distance} | ${out.state}`);
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

  // ---- 8. freehand stays local and keeps its exact draft shape ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async () => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const at = i => {
        const s = proj.containerPointFromCoords(
          new kakao.maps.LatLng(initialEditPath[i][1], initialEditPath[i][2]));
        return { x: s.x, y: s.y };
      };
      const fire = (type, pt) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 5, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      const sent = [];
      window.fetch = async (url, opts) => {
        sent.push(JSON.parse(opts.body));
        return { ok: true, json: async () => ({ preview_url: '#unexpected' }) };
      };
      // Make a gap first.
      document.getElementById('eraserTool').click();
      fire('pointerdown', at(10));
      for (const i of [12, 14, 16, 18, 20]) fire('pointermove', at(i));
      fire('pointerup', at(20));
      document.getElementById('selErase').click();

      document.getElementById('drawTool').click();
      const pressed = document.getElementById('drawTool').getAttribute('aria-pressed');
      const draggable = window.__map.draggable;

      const red=(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#e5322e').pop();
      const rs=proj.containerPointFromCoords(red._o.path[0]);
      const re=proj.containerPointFromCoords(red._o.path[red._o.path.length-1]);
      const start={x:rs.x,y:rs.y},end={x:re.x,y:re.y};
      fire('pointerdown', start);
      for (const t of [.2, .4, .6, .8])
        fire('pointermove', { x:start.x+(end.x-start.x)*t+18*Math.sin(t*Math.PI),
          y:start.y+(end.y-start.y)*t-24*Math.sin(t*Math.PI) });
      fire('pointerup', end);
      await new Promise(r => setTimeout(r, 60));
      const blue = (window.__lines || []).filter(
        l => l._map && l._o.strokeColor === '#1668dc').pop();
      const bp=blue&&blue._o.path;
      const rp=red&&red._o.path;
      const metres=(a,b)=>Math.hypot((a.getLat()-b.getLat())*111320,(a.getLng()-b.getLng())*88800);
      const state=document.getElementById('editDraftState').textContent;
      return { pressed, draggable, sent, strokes:(window.__lines || []).filter(
          l=>l._map&&l._o.strokeColor==='#1668dc').length,
        bluePoints:blue ? blue._o.path.length : 0,
        connection:state==='경로 확인 필요',
        joins:bp&&rp?[metres(bp[0],rp[0]),metres(bp[bp.length-1],rp[rp.length-1])]:[],
        distance:document.getElementById('editDistance').textContent,
        state };
    });

    check('drawing tool engages', out.pressed === 'true', out.pressed);
    check('the map stops panning while freehand is active',
      out.draggable === false, `draggable=${out.draggable}`);
    check('drawing makes no request before save', out.sent.length === 0, JSON.stringify(out.sent));
    check('the exact freehand stroke remains visible',
      out.strokes === 1 && out.bluePoints >= 5, JSON.stringify(out));
    check('a stroke touching both red ends becomes preview-ready',
      out.connection === true && out.state === '경로 확인 필요', JSON.stringify(out));
    check('drafting never invents a new distance', /미완성/.test(out.distance), out.distance);
    check('no page errors while drawing', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  // ---- 8b. multiple strokes can continue an unfinished freehand draft ----
  {
    const { p } = await page(browser, 'harness.html');
    const undos = await p.evaluate(async () => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const at = i => {
        const s = proj.containerPointFromCoords(
          new kakao.maps.LatLng(initialEditPath[i][1], initialEditPath[i][2]));
        return { x:s.x, y:s.y };
      };
      const fire = (type, pt) => overlay.dispatchEvent(new PointerEvent(type, {
        pointerId: 6, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      document.getElementById('eraserTool').click();
      fire('pointerdown', at(10));fire('pointermove', at(15));fire('pointerup', at(20));
      document.getElementById('selErase').click();
      document.getElementById('drawTool').click();
      const red=(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#e5322e').pop();
      const redStart=red._o.path[0],redEnd=red._o.path[red._o.path.length-1];
      const startPoint=proj.containerPointFromCoords(redStart),endPoint=proj.containerPointFromCoords(redEnd);
      const start={x:startPoint.x,y:startPoint.y},end={x:endPoint.x,y:endPoint.y};
      const mid={x:start.x+(end.x-start.x)*.25,y:start.y+(end.y-start.y)*.25};
      fire('pointerdown',start);fire('pointermove',mid);fire('pointerup',mid);
      fire('pointerdown',mid);fire('pointermove',end);fire('pointerup',end);
      const state=()=>document.getElementById('editDraftState').textContent;
      const strokes=()=>(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#1668dc').length;
      const hasGap=()=>(window.__lines||[]).some(l=>l._map&&l._o.strokeColor==='#e5322e');
      const before={strokes:strokes(),ready:state()==='경로 확인 필요'};
      document.getElementById('editUndo').click();
      const afterOne={strokes:strokes(),ready:state()==='경로 확인 필요'};
      document.getElementById('editUndo').click();
      const afterTwo={strokes:strokes(),ready:state()==='경로 확인 필요',gap:hasGap()};
      return {before,afterOne,afterTwo};
    });
    check('separate strokes can complete one route draft',
      undos.before.strokes === 2 && undos.before.ready === true, JSON.stringify(undos));
    check('one undo removes only the last stroke',
      undos.afterOne.strokes === 1 && undos.afterOne.ready === false, JSON.stringify(undos));
    check('a second undo keeps the red gap but removes the first stroke',
      undos.afterTwo.strokes === 0 && !!undos.afterTwo.gap, JSON.stringify(undos));
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
    const blocked = await p.evaluate(async () => {
      const overlay=document.getElementById('editOverlay');overlay.setPointerCapture=()=>{};
      const proj=window.__map.getProjection();
      const at=i=>{const s=proj.containerPointFromCoords(new kakao.maps.LatLng(initialEditPath[i][1],initialEditPath[i][2]));return {x:s.x,y:s.y};};
      const fire=(type,pt)=>overlay.dispatchEvent(new PointerEvent(type,{pointerId:11,bubbles:true,
        clientX:overlay.getBoundingClientRect().left+pt.x,clientY:overlay.getBoundingClientRect().top+pt.y}));
      let body = null;
      window.fetch=async(url,opts)=>{body=JSON.parse(opts.body);return {ok:true,json:async()=>({preview_url:'#saved'})};};
      document.getElementById('eraserTool').click();
      fire('pointerdown',at(10));fire('pointermove',at(15));fire('pointerup',at(20));
      document.getElementById('selErase').click();
      document.getElementById('drawTool').click();
      // A middle fragment touches neither red endpoint.
      fire('pointerdown',at(13));fire('pointermove',at(15));fire('pointerup',at(17));
      document.getElementById('editSave').click();
      await new Promise(r=>setTimeout(r,40));
      return {body,sheetHidden:document.getElementById('nameSheet').hidden,
        error:document.getElementById('editToastText').textContent};
    });
    check('an unconnected draft is rejected before the name sheet',
      blocked.body === null && blocked.sheetHidden === true && /이어지지|양 끝/.test(blocked.error),
      JSON.stringify(blocked));
    await p.close();
  }
  {
    const { p } = await page(browser, 'harness.html');
    const flow = await p.evaluate(async () => {
      const overlay=document.getElementById('editOverlay');overlay.setPointerCapture=()=>{};
      const proj=window.__map.getProjection();
      const at=i=>{const s=proj.containerPointFromCoords(new kakao.maps.LatLng(initialEditPath[i][1],initialEditPath[i][2]));return {x:s.x,y:s.y};};
      const fire=(type,pt)=>overlay.dispatchEvent(new PointerEvent(type,{pointerId:12,bubbles:true,
        clientX:overlay.getBoundingClientRect().left+pt.x,clientY:overlay.getBoundingClientRect().top+pt.y}));
      const bodies=[];
      window.fetch=async(url,opts)=>{
        const body=JSON.parse(opts.body);bodies.push(body);
        if(body.action==='snap')return {ok:true,json:async()=>({
          path:initialEditPath,geometry:initialEditGeometry,length_km:5.31,summary:{
            course_id:'preview',title:'5.3km 도보 미리보기',name_placeholder:'도보 미리보기런',
            length_km:5.31,ascent_m:40,elev_range:[10,30],signals:5,
            facility_counts:{convenience_store:3,restroom:1},facility_rows:[],
            traits:[],highlights:[],cautions:[],badges:[]}})};
        return {ok:true,json:async()=>({preview_url:'#saved'})};
      };
      document.getElementById('eraserTool').click();
      fire('pointerdown',at(10));fire('pointermove',at(15));fire('pointerup',at(20));
      document.getElementById('selErase').click();
      document.getElementById('drawTool').click();
      const red=(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#e5322e').pop();
      const rs=proj.containerPointFromCoords(red._o.path[0]);
      const re=proj.containerPointFromCoords(red._o.path[red._o.path.length-1]);
      const start={x:rs.x,y:rs.y},end={x:re.x,y:re.y};
      fire('pointerdown',start);
      fire('pointermove',{x:(start.x+end.x)/2,y:(start.y+end.y)/2});
      fire('pointerup',end);
      document.getElementById('editSave').click();
      await new Promise(r => setTimeout(r, 80));
      const preview={
        sheetHidden:document.getElementById('nameSheet').hidden,
        state:document.getElementById('editDraftState').textContent,
        label:document.getElementById('editSaveLabel').textContent,
        error:document.getElementById('editToastText').textContent,
        blue:(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#1668dc').length,
        red:(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#e5322e').length
      };
      document.getElementById('editSave').click();
      const input = document.getElementById('nameSheetInput');
      input.value = 'AA런';
      document.getElementById('nameSheetSave').click();
      await new Promise(r => setTimeout(r, 120));
      return {bodies,preview};
    });
    check('a connected draft previews a walkable route before naming',
      flow.bodies[0] && flow.bodies[0].action === 'snap' && flow.bodies[0].stroke.length >= 2 &&
      flow.preview.sheetHidden === true && flow.preview.state === '경로 확인됨' &&
      flow.preview.label === '저장' && flow.preview.blue === 0 && flow.preview.red === 0,
      JSON.stringify(flow));
    check('only the reviewed snapped path is saved with a name',
      flow.bodies[1] && flow.bodies[1].action === 'save' &&
      !('stroke' in flow.bodies[1]) && flow.bodies[1].name === 'AA런', JSON.stringify(flow.bodies));
    await p.close();
  }

  // ---- 10. reset stays in the editor and its discarded draft is undoable ----
  {
    const { p, errors } = await page(browser, 'harness.html');
    const out = await p.evaluate(async () => {
      const overlay = document.getElementById('editOverlay');
      overlay.setPointerCapture = () => {};
      const proj = window.__map.getProjection();
      const at=i=>{const s=proj.containerPointFromCoords(
        new kakao.maps.LatLng(initialEditPath[i][1],initialEditPath[i][2]));return {x:s.x,y:s.y};};
      const fire = (t, pt) => overlay.dispatchEvent(new PointerEvent(t, {
        pointerId: 9, bubbles: true,
        clientX: overlay.getBoundingClientRect().left + pt.x,
        clientY: overlay.getBoundingClientRect().top + pt.y }));
      document.getElementById('eraserTool').click();
      fire('pointerdown',at(10));fire('pointermove',at(15));fire('pointerup',at(20));
      document.getElementById('selErase').click();
      document.getElementById('drawTool').click();
      const red=(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#e5322e').pop();
      const rs=proj.containerPointFromCoords(red._o.path[0]);
      const re=proj.containerPointFromCoords(red._o.path[red._o.path.length-1]);
      const start={x:rs.x,y:rs.y},end={x:re.x,y:re.y};
      fire('pointerdown',start);fire('pointermove',{x:(start.x+end.x)/2,y:(start.y+end.y)/2});fire('pointerup',end);
      const counts=()=>({gap:(window.__lines||[]).some(l=>l._map&&l._o.strokeColor==='#e5322e'),
        strokes:(window.__lines||[]).filter(l=>l._map&&l._o.strokeColor==='#1668dc').length});
      const drafted=counts();
      document.getElementById('editCancel').click();
      await new Promise(r => setTimeout(r, 40));
      const reset={editing:document.body.classList.contains('editing'),...counts(),
        distance:document.getElementById('editDistance').textContent,
        state:document.getElementById('editDraftState').textContent};
      document.getElementById('editToastAction').click();
      await new Promise(r => setTimeout(r, 40));
      const restored={...counts(),
        state:document.getElementById('editDraftState').textContent};
      return {drafted,reset,restored,original:initialLengthKm.toFixed(2)+'km'};
    });
    check('reset restores the original route but remains in editing',
      out.reset.editing && out.reset.gap === false && out.reset.strokes === 0 &&
      out.reset.distance === out.original && out.reset.state === '편집 준비', JSON.stringify(out));
    check('undoing reset restores the red gap and freehand stroke',
      out.restored.gap && out.restored.strokes === out.drafted.strokes && out.restored.state === '경로 확인 필요',
      JSON.stringify(out));
    check('no page errors while resetting', errors.length === 0, errors.join(' | '));
    await p.close();
  }

  await browser.close();

  const failed = results.filter(r => !r.ok);
  for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  — ' + r.detail : ''}`);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
})();
