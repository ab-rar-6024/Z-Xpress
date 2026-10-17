/* ═══════════════════════════════════════════════
   ENTER KEY → MOVE TO NEXT FIELD
   Works inside any <form>. On the LAST field it
   clicks the submit button instead.
═══════════════════════════════════════════════ */
document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    const el = e.target;
    // Textareas keep normal behaviour
    if (el.tagName === "TEXTAREA") return;
    // Buttons do nothing here
    if (el.tagName === "BUTTON" || el.type === "submit") return;
    // If an autocomplete dropdown is open, let it handle Enter
    const wrap = el.closest(".ac-wrap");
    if (wrap) {
        const drop = wrap.querySelector(".ac-drop");
        if (drop && drop.classList.contains("open")) return;
    }
    const form = el.closest("form");
    if (!form) return;
    const focusable = [...form.querySelectorAll(
        "input:not([type=hidden]):not([readonly]):not([disabled]), select, textarea"
    )];
    const idx = focusable.indexOf(el);
    if (idx === -1) return;
    e.preventDefault();
    if (idx < focusable.length - 1) {
        focusable[idx + 1].focus();
    } else {
        // last field → submit
        const btn = form.querySelector("button[type=submit], input[type=submit], .btn-submit");
        if (btn) btn.click();
    }
});


/* ═══════════════════════════════════════════════
   AUTOCOMPLETE ENGINE
   Usage:
     bindAC(inputEl, "/api/ac/endpoint?q=",
            item => { inputEl.value = item; },
            item => String(item),          // display label
            item => String(item.sub||"")   // optional sub-line
     );
═══════════════════════════════════════════════ */
function closeAllAC(except) {
    document.querySelectorAll(".ac-drop.open").forEach(d => {
        if (d !== except) d.classList.remove("open");
    });
}
document.addEventListener("click", e => {
    if (!e.target.closest(".ac-wrap")) closeAllAC();
});

function bindAC(inputEl, url, onSelect, labelFn, subFn) {
    // Ensure there is a drop div inside the same .ac-wrap
    let wrap = inputEl.closest(".ac-wrap");
    if (!wrap) {
        // auto-wrap if not already wrapped
        wrap = document.createElement("div");
        wrap.className = "ac-wrap";
        inputEl.parentNode.insertBefore(wrap, inputEl);
        wrap.appendChild(inputEl);
    }
    let drop = wrap.querySelector(".ac-drop");
    if (!drop) {
        drop = document.createElement("div");
        drop.className = "ac-drop";
        wrap.appendChild(drop);
    }

    let timer, hiIdx = -1;
    let lastItems = [];

    function render(items) {
        lastItems = items;
        if (!items.length) { drop.classList.remove("open"); return; }
        drop.innerHTML = "";
        hiIdx = 0;
        items.forEach((item, i) => {
            const div = document.createElement("div");
            div.className = "ac-item" + (i === 0 ? " hi" : "");
            const lbl = labelFn ? labelFn(item) : String(item);
            const sub = subFn  ? subFn(item)   : "";
            div.innerHTML = lbl + (sub ? `<div class="ac-sub">${sub}</div>` : "");
            div.addEventListener("mousedown", ev => {
                ev.preventDefault();
                choose(item);
            });
            drop.appendChild(div);
        });
        drop.classList.add("open");
        closeAllAC(drop);
    }

    function choose(item) {
        onSelect(item);
        drop.classList.remove("open");
        hiIdx = -1;
    }

    function moveHi(d) {
        const els = drop.querySelectorAll(".ac-item");
        if (!els.length) return;
        els[hiIdx]?.classList.remove("hi");
        hiIdx = Math.max(0, Math.min(els.length - 1, (hiIdx < 0 ? 0 : hiIdx) + d));
        els[hiIdx]?.classList.add("hi");
        els[hiIdx]?.scrollIntoView({ block: "nearest" });
    }

    inputEl.addEventListener("input", () => {
        clearTimeout(timer);
        const q = inputEl.value.trim();
        if (q.length < 1) { drop.classList.remove("open"); return; }
        timer = setTimeout(async () => {
            try {
                const res = await fetch(url + encodeURIComponent(q));
                render(await res.json());
            } catch { /* network error */ }
        }, 80);
    });

    inputEl.addEventListener("keydown", e => {
        if (!drop.classList.contains("open")) return;
        if (e.key === "ArrowDown")  { e.preventDefault(); moveHi(1); }
        else if (e.key === "ArrowUp")   { e.preventDefault(); moveHi(-1); }
        else if (e.key === "Enter") {
            const hi = drop.querySelector(".ac-item.hi");
            if (hi) { e.preventDefault(); e.stopImmediatePropagation(); hi.dispatchEvent(new MouseEvent("mousedown")); }
        }
        else if (e.key === "Escape") { drop.classList.remove("open"); }
    });

    inputEl.addEventListener("blur", () => {
        setTimeout(() => drop.classList.remove("open"), 160);
    });
}


/* ═══════════════════════════════════════════════
   SIDEBAR TOGGLE
═══════════════════════════════════════════════ */
function toggleSidebar() {
    document.getElementById("sidebar").classList.toggle("collapsed");
}


/* ═══════════════════════════════════════════════
   CONFIRM DELETE
═══════════════════════════════════════════════ */
function confirmDel(formId) {
    if (confirm("Delete this record? This cannot be undone.")) {
        document.getElementById(formId).submit();
    }
}
