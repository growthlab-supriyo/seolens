<script>
/* SEO Lens: download the current map view (link map or folder tree) as a PNG of the whole drawing, at twice screen sharpness. */
(function () {
  var bar = document.querySelector(".bar");
  if (!bar || document.getElementById("pngBtn")) return;
  // Drawn while hidden (a dashboard tab), the map fits a zero-size window: redraw the current view once it has a real size.
  var stage = document.getElementById("stage"), lastW = 0, timer = null;
  if (stage && window.ResizeObserver) {
    new ResizeObserver(function () {
      var w = stage.getBoundingClientRect().width;
      if (w > 50 && Math.abs(w - lastW) > 40) {
        var first = lastW === 0;
        lastW = w;
        clearTimeout(timer);
        timer = setTimeout(function () {
          var on = document.querySelector(".bar button.on");
          if (on && window.data) on.click();
        }, first ? 50 : 300);
      }
    }).observe(stage);
  }
  var b = document.createElement("button");
  b.id = "pngBtn";
  b.textContent = "⬇ Download PNG";
  b.title = "Save the whole map (not only the visible part) as an image";
  bar.appendChild(b);
  b.onclick = function () {
    var svg = document.querySelector("#stage svg");
    if (!svg) return;
    var g = svg.querySelector(":scope > g");
    var bb = g.getBBox(), pad = 30, head = 46, scale = 2;
    var w = Math.ceil(bb.width + pad * 2), h = Math.ceil(bb.height + pad * 2 + head);
    var clone = svg.cloneNode(true), cg = clone.querySelector(":scope > g");
    cg.setAttribute("transform", "translate(" + (pad - bb.x) + "," + (pad + head - bb.y) + ")");
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", w); clone.setAttribute("height", h);
    clone.setAttribute("viewBox", "0 0 " + w + " " + h);
    clone.style.width = w + "px"; clone.style.height = h + "px";
    var css = Array.prototype.map.call(document.querySelectorAll("style"), function (s) { return s.textContent; }).join("\n");
    var st = document.createElementNS("http://www.w3.org/2000/svg", "style");
    st.textContent = css + " text{font-family:Arial,sans-serif}";
    clone.insertBefore(st, clone.firstChild);
    var bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    bg.setAttribute("width", w); bg.setAttribute("height", h); bg.setAttribute("fill", "#ffffff");
    clone.insertBefore(bg, st.nextSibling);
    var title = document.createElementNS("http://www.w3.org/2000/svg", "text");
    var view = document.getElementById("vTree").className === "on" ? "Folder tree" : "Link map by click depth";
    title.setAttribute("x", pad); title.setAttribute("y", 30);
    title.setAttribute("style", "font:bold 18px Arial,sans-serif;fill:#202124");
    title.textContent = "Site architecture map · " + ((window.data && data.host) || "") + " · " + view + " · SEO Lens";
    clone.insertBefore(title, bg.nextSibling);
    var src = new XMLSerializer().serializeToString(clone);
    var img = new Image();
    img.onload = function () {
      var c = document.createElement("canvas");
      c.width = w * scale; c.height = h * scale;
      var ctx = c.getContext("2d");
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      c.toBlob(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "site-architecture-map-" + (((window.data && data.host) || "site").replace(/[^a-z0-9.-]+/gi, "-")) + "-" +
                     (view === "Folder tree" ? "folder-tree" : "link-map") + ".png";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
      }, "image/png");
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(src);
  };
})();
</script>
