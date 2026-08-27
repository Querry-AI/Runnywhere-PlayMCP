/** Hardware-style browser touch input; never dispatch directly onto an SVG. */
const { chromium } = require('playwright');
const path = require('path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch();
  try {
    for (const width of [390, 320]) {
      const context = await browser.newContext({viewport:{width,height:844},hasTouch:true,isMobile:true});
      const page = await context.newPage();
      const errors=[];page.on('pageerror',e=>errors.push(String(e)));
      await page.goto('file://' + path.join(process.argv[2] || '/tmp/runart-harness','harness.html'));
      const box=await page.locator('#map').boundingBox();
      const x=width/2,y=box.y+box.height*.45;
      assert.equal(await page.evaluate(([x,y])=>document.elementFromPoint(x,y).id,[x,y]),'mapPanSurface');
      // A projection whose pixels change with both centre and level catches
      // stale-projection and pinch-anchor bugs the identity double misses.
      await page.evaluate(()=>{
        window.__touchTrace=[];
        for(const type of ['touchstart','touchmove','touchend','touchcancel'])
          document.addEventListener(type,e=>window.__touchTrace.push({type,target:e.target.id,
            ids:Array.from(e.targetTouches).map(t=>t.identifier)}),true);
        window.__map.getProjection=function(){
          const center=this.getCenter(),scale=100000*Math.pow(2,6-this.getLevel());
          const rect=document.getElementById('map').getBoundingClientRect();
          return {
            containerPointFromCoords:p=>new kakao.maps.Point(rect.width/2+(p.getLng()-center.getLng())*scale,
              rect.height/2-(p.getLat()-center.getLat())*scale),
            coordsFromContainerPoint:p=>new kakao.maps.LatLng(center.getLat()-(p.y-rect.height/2)/scale,
              center.getLng()+(p.x-rect.width/2)/scale)
          };
        };
      });
      const cdp=await context.newCDPSession(page);
      const touch=(type,points)=>cdp.send('Input.dispatchTouchEvent',{type,touchPoints:points.map(([id,x,y])=>({id,x,y}))});
      const state=()=>page.evaluate(()=>({lat:__map.getCenter().getLat(),lon:__map.getCenter().getLng(),
        level:__map.getLevel(),moves:__map.centerCount||0,scroll:window.scrollY}));
      const before=await state();
      await touch('touchStart',[[1,x,y]]);
      await touch('touchMove',[[1,x+25,y+20]]);
      await touch('touchMove',[[1,x+50,y+40]]);
      const panned=await state();
      assert(panned.lon<before.lon && panned.lat>before.lat,'map follows finger right/down');
      // Add a second finger, spread, then pinch back without lifting both.
      await touch('touchStart',[[1,x-25,y],[2,x+25,y]]);
      await touch('touchMove',[[1,x-70,y],[2,x+70,y]]);
      const zoomed=await state();assert(zoomed.level<panned.level,'pinch out zooms in');
      await touch('touchMove',[[1,x-15,y],[2,x+15,y]]);
      assert((await state()).level>zoomed.level,'pinch in zooms out');
      // CDP touchEnd names the lifted point, not the remaining touch.
      await touch('touchEnd',[[2,x+15,y]]);
      const single=await state();
      await touch('touchMove',[[1,x+20,y+20]]);
      await page.waitForTimeout(80);
      assert((await state()).moves>single.moves,'remaining finger immediately resumes pan: '+
        JSON.stringify(await page.evaluate(()=>window.__touchTrace)));
      await touch('touchCancel',[]);
      const canceled=await state();
      await touch('touchStart',[[3,x,y]]);await touch('touchMove',[[3,x-30,y-20]]);await touch('touchEnd',[]);
      assert((await state()).moves>canceled.moves,'pan works after cancel');
      assert.equal((await state()).scroll,before.scroll,'map touches never scroll page');
      // Real toolbar taps must still switch ownership back to the tools.
      for(const tool of ['eraserTool','drawTool']) {
        await page.locator('#'+tool).tap();
        assert.equal(await page.locator('#mapPanSurface').isVisible(),false);
        await page.locator('#panTool').tap();
        assert.equal(await page.locator('#mapPanSurface').isVisible(),true);
        const old=await state();
        await touch('touchStart',[[4,x,y]]);await touch('touchMove',[[4,x+30,y]]);await touch('touchEnd',[]);
        assert((await state()).moves>old.moves,'pan works after switching tool');
      }
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}px: hit testing, drag, pinch in/out, pinch-to-pan, cancel, tool switch`);
      await context.close();
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
