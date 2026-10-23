"""Full-screen page viewer with its own pinch zoom.

The app itself does not zoom (layout.py sets the viewport to a fixed scale):
pinch-zooming the whole UI stuttered even on fast phones, and a page render
in a narrow panel was unreadable without it. This viewer zooms only the pages.

Opened from JS:
  window.__pbViewer.open(frames, start)  — clones of `.pb-src-page` frames
      (the Source tab, highlights included), start = 0-based index
  window.__pbViewer.openDoc(docId, pageCount, start)  — every page of a
      document through the access-checked /api/doc-image route

Zoom: two-finger pinch, double tap, Ctrl+wheel or the +/- buttons. While the
fingers move, the pages are only scaled with a CSS transform; on release the
zoom is committed as a width, so native scrolling pans the zoomed pages.
Back (Android) closes the viewer.
"""

PAGE_VIEWER_HTML = """<style>
.pbv { position: fixed; inset: 0; z-index: 9000; display: none; flex-direction: column;
       background: rgba(10, 12, 18, .97); }
.pbv.is-open { display: flex; }
/* The backdrop is dark in both themes, so the bar's colour is fixed too —
   a theme token turned it dark grey on black in light mode. */
.pbv-bar { display: flex; align-items: center; gap: 4px; padding: 6px 8px;
           color: #e6e8ec; font-size: .8rem; flex-shrink: 0; }
.pbv-bar .pbv-page { flex: 1; text-align: center; white-space: nowrap; }
.pbv-btn { background: none; border: 0; color: inherit; width: 40px; height: 40px;
           border-radius: 50%; display: flex; align-items: center; justify-content: center;
           cursor: pointer; }
.pbv-btn:active { background: rgba(255, 255, 255, .1); }
.pbv-btn .material-icons { font-size: 22px; }
.pbv-scroll { flex: 1; overflow: auto; touch-action: pan-x pan-y;
              -webkit-overflow-scrolling: touch; overscroll-behavior: contain; }
.pbv-inner { width: 100%; transform-origin: 0 0; will-change: transform; }
.pbv-inner .pb-src-page { margin: 0 0 8px 0 !important; cursor: default; }
.pbv-inner img { display: block; width: 100%; height: 100%; object-fit: contain; }
.pb-src-page, .dl-thumb { cursor: zoom-in; }
</style>
<script>
(function () {
  if (window.__pbViewer) return;
  var el, scroll, inner, label, zoom = 1, frames = [];

  function build() {
    el = document.createElement('div');
    el.className = 'pbv';
    el.innerHTML =
      '<div class="pbv-bar">' +
      '<button class="pbv-btn" data-a="out" aria-label="-"><span class="material-icons">zoom_out</span></button>' +
      '<button class="pbv-btn" data-a="in" aria-label="+"><span class="material-icons">zoom_in</span></button>' +
      '<span class="pbv-page"></span>' +
      '<button class="pbv-btn" data-a="close" aria-label="x"><span class="material-icons">close</span></button>' +
      '</div><div class="pbv-scroll"><div class="pbv-inner"></div></div>';
    document.body.appendChild(el);
    scroll = el.querySelector('.pbv-scroll');
    inner = el.querySelector('.pbv-inner');
    label = el.querySelector('.pbv-page');
    el.querySelector('.pbv-bar').addEventListener('click', function (e) {
      var b = e.target.closest('[data-a]');
      if (!b) return;
      var a = b.getAttribute('data-a');
      var r = scroll.getBoundingClientRect();
      if (a === 'close') close();
      else zoomAt(a === 'in' ? zoom * 1.5 : zoom / 1.5, r.left + r.width / 2, r.top + r.height / 2);
    });
    scroll.addEventListener('scroll', track, { passive: true });
    pinch();
    scroll.addEventListener('wheel', function (e) {
      if (!e.ctrlKey) return;
      e.preventDefault();
      zoomAt(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX, e.clientY);
    }, { passive: false });
    window.addEventListener('popstate', function () { if (el.classList.contains('is-open')) hide(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && el.classList.contains('is-open')) close();
    });
  }

  function clamp(z) { return Math.min(6, Math.max(1, z)); }

  // Commit a zoom so that the content point under (x, y) stays under it.
  function zoomAt(z, x, y) {
    z = clamp(z);
    var r = scroll.getBoundingClientRect();
    var cx = scroll.scrollLeft + x - r.left, cy = scroll.scrollTop + y - r.top;
    var s = z / zoom;
    zoom = z;
    inner.style.width = (zoom * 100) + '%';
    scroll.scrollLeft = cx * s - (x - r.left);
    scroll.scrollTop = cy * s - (y - r.top);
  }

  function pinch() {
    var d0 = 0, mx = 0, my = 0, ox = 0, oy = 0, s = 1, lastTap = 0;
    function dist(t) { return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY); }
    scroll.addEventListener('touchstart', function (e) {
      if (e.touches.length === 2) {
        d0 = dist(e.touches); s = 1;
        mx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        my = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        var r = scroll.getBoundingClientRect();
        ox = scroll.scrollLeft + mx - r.left;
        oy = scroll.scrollTop + my - r.top;
        inner.style.transformOrigin = ox + 'px ' + oy + 'px';
      } else if (e.touches.length === 1) {
        var now = Date.now(), t = e.touches[0];
        if (now - lastTap < 300) {
          e.preventDefault();
          zoomAt(zoom > 1.2 ? 1 : 2.5, t.clientX, t.clientY);
          lastTap = 0;
        } else lastTap = now;
      }
    }, { passive: false });
    scroll.addEventListener('touchmove', function (e) {
      if (e.touches.length !== 2 || !d0) return;
      e.preventDefault();
      s = clamp(zoom * dist(e.touches) / d0) / zoom;
      inner.style.transform = 'scale(' + s + ')';
    }, { passive: false });
    scroll.addEventListener('touchend', function (e) {
      if (!d0 || e.touches.length >= 2) return;
      d0 = 0;
      inner.style.transform = '';
      inner.style.transformOrigin = '0 0';
      if (Math.abs(s - 1) > 0.01) zoomAt(zoom * s, mx, my);
      s = 1;
    });
  }

  function track() {
    var top = scroll.getBoundingClientRect().top + scroll.clientHeight / 3, cur = 1;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].getBoundingClientRect().top <= top) cur = i + 1;
    }
    label.textContent = cur + ' / ' + frames.length;
  }

  function show(list, start) {
    if (!el) build();
    inner.innerHTML = '';
    zoom = 1;
    inner.style.width = '100%';
    frames = list;
    list.forEach(function (f) { inner.appendChild(f); });
    el.classList.add('is-open');
    history.pushState({ pbv: 1 }, '');
    requestAnimationFrame(function () {
      var f = frames[Math.max(0, Math.min(start || 0, frames.length - 1))];
      scroll.scrollTop = f ? f.offsetTop : 0;
      track();
    });
  }

  function hide() {
    el.classList.remove('is-open');
    inner.innerHTML = '';
    frames = [];
  }

  function close() {
    if (history.state && history.state.pbv) history.back();
    else hide();
  }

  window.__pbViewer = {
    open: function (nodes, start) {
      show(Array.prototype.map.call(nodes, function (n) {
        var c = n.cloneNode(true);
        c.querySelectorAll('img[loading]').forEach(function (i) { i.loading = 'eager'; });
        return c;
      }), start);
    },
    openDoc: function (docId, count, start) {
      var list = [];
      for (var n = 1; n <= count; n++) {
        var f = document.createElement('div');
        f.className = 'pb-src-page';
        f.style.cssText = 'position:relative;background:#fff;aspect-ratio:0.7071;';
        var img = document.createElement('img');
        img.src = '/api/doc-image/' + docId + '/' + n;
        img.loading = Math.abs(n - 1 - (start || 0)) <= 1 ? 'eager' : 'lazy';
        img.onload = function () {
          if (this.naturalWidth) this.parentNode.style.aspectRatio = this.naturalWidth / this.naturalHeight;
        };
        f.appendChild(img);
        list.push(f);
      }
      show(list, start);
    },
  };

  // A page in the Source tab opens the viewer on that page.
  document.addEventListener('click', function (e) {
    var page = e.target.closest && e.target.closest('.pb-src-scroll .pb-src-page');
    if (!page) return;
    var all = page.parentNode.querySelectorAll('.pb-src-page');
    window.__pbViewer.open(all, Array.prototype.indexOf.call(all, page));
  });
})();
</script>"""
