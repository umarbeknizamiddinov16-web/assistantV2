(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const CIRC = 2 * Math.PI * 52;
  const nf = new Intl.NumberFormat("ru-RU");

  const state = { profile: null, account: null, keywords: [], searching: false };

  /* ---------- утилиты ---------- */
  function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) el.append(kid);
    return el;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    let body = null;
    try { body = await res.json(); } catch { /* не JSON */ }
    if (!res.ok) {
      const d = body && body.detail;
      const msg = typeof d === "string" ? d : Array.isArray(d) ? "Проверьте введённые данные." : "Не удалось выполнить запрос.";
      const err = new Error(msg);
      err.status = res.status;
      if (res.status === 401 && !path.startsWith("/api/auth/")) onSessionExpired();
      throw err;
    }
    return body;
  }

  function toast(text) {
    const t = h("div", { class: "toast", text });
    $("#toasts").append(t);
    setTimeout(() => t.remove(), 4200);
  }

  function showMsg(el, text, ok = false) {
    el.textContent = text;
    el.classList.toggle("ok", ok);
    el.hidden = !text;
  }

  function busy(btn, on) {
    btn.classList.toggle("is-busy", on);
    btn.disabled = on;
  }

  /* ---------- кольцо совпадения ---------- */
  const tone = (s) => (s >= 70 ? "high" : s >= 40 ? "mid" : "low");
  const verdict = (s) => (s >= 70 ? "Сильное совпадение" : s >= 40 ? "Частичное совпадение" : "Слабое совпадение");

  function animateDial(dial, score, delay = 0) {
    const bar = $(".dial__bar", dial);
    const num = $(".dial__num", dial);
    dial.dataset.tone = tone(score) === "high" ? "" : tone(score);
    const setNum = (v) => { num.replaceChildren(String(v), h("small", { text: "%" })); };
    const target = CIRC * (1 - score / 100);
    if (reduceMotion) { bar.style.strokeDashoffset = target; setNum(score); return; }
    const dur = 1100;
    setTimeout(() => {
      bar.style.transition = `stroke-dashoffset ${dur}ms cubic-bezier(.2,.8,.2,1)`;
      bar.style.strokeDashoffset = target;
      const t0 = performance.now();
      const tick = (t) => {
        const p = Math.min(1, (t - t0) / dur);
        setNum(Math.round(score * (1 - Math.pow(1 - p, 3))));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, delay);
  }

  function makeDial(score) {
    const d = h("div", { class: "dial", role: "img", "aria-label": `Совпадение ${score}%` },
      (() => {
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("viewBox", "0 0 120 120");
        svg.setAttribute("aria-hidden", "true");
        for (const cls of ["dial__track", "dial__bar"]) {
          const c = document.createElementNS(ns, "circle");
          c.setAttribute("class", cls);
          c.setAttribute("cx", "60"); c.setAttribute("cy", "60"); c.setAttribute("r", "52");
          svg.append(c);
        }
        return svg;
      })(),
      h("span", { class: "dial__num", "aria-hidden": "true" }, "0", h("small", { text: "%" })));
    requestAnimationFrame(() => animateDial(d, score, 120));
    return d;
  }

  /* ---------- профиль ---------- */
  function setStatus(id, on, pop = false) {
    const el = $(id);
    el.hidden = !on;
    if (on && pop) { el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); }
  }

  function renderResume(r, pop = false) {
    setStatus("#resume-status", r.has, pop);
    // одно резюме: пока оно есть, зоны загрузки и ввода текста скрыты
    $("#resume-editor").hidden = r.has;
    $("#resume-card").hidden = !r.has;
    if (r.has) {
      $("#resume-name").textContent = r.name || "Резюме, введённое текстом";
      $("#resume-meta").textContent = `${nf.format(r.chars)} символов`;
      $("#resume-preview").textContent = r.preview + (r.chars > r.preview.length ? "…" : "");
    }
  }

  function renderFilters(f, options, pop = false) {
    state.keywords = [...f.keywords];
    renderTags();
    setStatus("#filters-status", f.keywords.length > 0, pop);

    const locs = [...options.locations, "Другой город"];
    const isKnown = options.locations.includes(f.location);
    const locValue = isKnown ? f.location : f.location ? "Другой город" : options.locations[0];
    buildSeg($("#loc-seg"), "loc", locs, locValue, (v) => {
      const custom = $("#loc-custom");
      custom.hidden = v !== "Другой город";
      if (!custom.hidden) custom.focus();
    });
    const custom = $("#loc-custom");
    custom.hidden = locValue !== "Другой город";
    custom.value = isKnown ? "" : f.location;

    buildSeg($("#emp-seg"), "emp", options.employment, f.employment_type || options.employment[0]);
  }

  function buildSeg(root, name, values, current, onChange) {
    root.replaceChildren(...values.map((v) => {
      const input = h("input", { type: "radio", name, value: v, checked: v === current });
      if (onChange) input.addEventListener("change", () => onChange(v));
      return h("label", {}, input, h("span", { text: v.charAt(0).toUpperCase() + v.slice(1) }));
    }));
  }

  const segValue = (name) => ($(`input[name="${name}"]:checked`) || {}).value;

  function applyProfile(p, pop = false) {
    state.profile = p;
    renderResume(p.resume, pop);
    renderFilters(p.filters, p.options, pop);
    $("#resume-next").disabled = !p.resume.has;
    updateSearchButton();
    updateChatAvailability();
  }

  function updateSearchButton(keepStatus = false) {
    const p = state.profile;
    const ready = p && p.resume.has && p.filters.keywords.length > 0;
    $("#search-btn").disabled = !ready || state.searching;
    // итоговое сообщение поиска («Ничего не нашли…», причина ошибки) нельзя затирать подсказкой «Запустите поиск»
    if (!keepStatus && !state.searching && !$("#cards").children.length) {
      $("#search-status").textContent = ready
        ? "Всё готово. Запустите поиск."
        : !p || !p.resume.has ? "Добавьте резюме и должности, затем запустите поиск." : "Осталось задать должности.";
    }
  }

  /* ---------- вкладки резюме ---------- */
  const tabs = [$("#tab-file"), $("#tab-text")];
  function selectTab(i) {
    tabs.forEach((t, j) => {
      t.setAttribute("aria-selected", String(i === j));
      t.tabIndex = i === j ? 0 : -1;
    });
    $("#pane-file").hidden = i !== 0;
    $("#pane-text").hidden = i !== 1;
    if (i === 1) $("#resume-text").focus();
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => selectTab(i));
    t.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") { const j = 1 - i; selectTab(j); tabs[j].focus(); }
    });
  });

  /* ---------- загрузка файла ---------- */
  const drop = $("#drop");
  const fileInput = $("#file");
  drop.addEventListener("pointermove", (e) => {
    const r = drop.getBoundingClientRect();
    drop.style.setProperty("--mx", `${e.clientX - r.left}px`);
    drop.style.setProperty("--my", `${e.clientY - r.top}px`);
  });
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("is-over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadFile(f); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) uploadFile(fileInput.files[0]); fileInput.value = ""; });

  async function uploadFile(file) {
    const msg = $("#resume-msg");
    showMsg(msg, "");
    if (state.profile && state.profile.resume.has) return showMsg(msg, "Резюме уже добавлено. Сначала удалите его.");
    if (!/\.(pdf|docx|txt)$/i.test(file.name)) return showMsg(msg, "Поддерживаются файлы .pdf, .docx и .txt");
    if (file.size > 5 * 1024 * 1024) return showMsg(msg, "Файл слишком большой (максимум 5 МБ)");
    drop.classList.add("is-loading");
    try {
      const p = await api(`/api/resume/file?filename=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
      });
      applyProfile(p, true);
      showMsg(msg, `Файл «${file.name}» загружен.`, true);
    } catch (e) { showMsg(msg, e.message); }
    finally { drop.classList.remove("is-loading"); }
  }

  $("#save-text").addEventListener("click", async (e) => {
    const btn = e.currentTarget, msg = $("#resume-msg");
    showMsg(msg, "");
    if (state.profile && state.profile.resume.has) return showMsg(msg, "Резюме уже добавлено. Сначала удалите его.");
    busy(btn, true);
    try {
      applyProfile(await api("/api/resume", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: $("#resume-text").value }),
      }), true);
      showMsg(msg, "Резюме сохранено.", true);
    } catch (err) { showMsg(msg, err.message); }
    finally { busy(btn, false); }
  });

  /* ---------- окна: просмотр и удаление ---------- */
  const resumeDialog = $("#resume-dialog");
  const confirmDialog = $("#confirm-dialog");

  function openDialog(d) {
    if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
  }
  function closeDialog(d) {
    if (typeof d.close === "function") d.close(); else d.removeAttribute("open");
  }
  // клик по затемнённому фону закрывает окно
  [resumeDialog, confirmDialog].forEach((d) => d.addEventListener("click", (e) => { if (e.target === d) closeDialog(d); }));

  $("#view-resume").addEventListener("click", async () => {
    const text = $("#resume-dialog-text"), meta = $("#resume-dialog-meta");
    text.textContent = "Загружаем…";
    meta.textContent = "";
    openDialog(resumeDialog);
    try {
      const r = await api("/api/resume");
      text.textContent = r.text;
      meta.textContent = `${r.name || "Резюме, введённое текстом"} · ${nf.format(r.chars)} символов`;
    } catch (err) { text.textContent = err.message; }
  });
  $("#resume-dialog-close").addEventListener("click", () => closeDialog(resumeDialog));

  $("#delete-resume").addEventListener("click", () => {
    const r = state.profile && state.profile.resume;
    $("#confirm-text").textContent = `Файл «${(r && r.name) || "Резюме"}» будет удалён. После этого можно будет добавить одно новое резюме.`;
    showMsg($("#confirm-msg"), "");
    openDialog(confirmDialog);
    $("#confirm-no").focus();
  });
  $("#confirm-no").addEventListener("click", () => closeDialog(confirmDialog));

  $("#confirm-yes").addEventListener("click", async (e) => {
    const btn = e.currentTarget, msg = $("#resume-msg");
    showMsg($("#confirm-msg"), "");
    busy(btn, true);
    try {
      const p = await api("/api/resume", { method: "DELETE" });
      $("#resume-text").value = "";
      closeDialog(confirmDialog);
      applyProfile(p, true);
      showMsg(msg, "Резюме удалено. Теперь можно добавить одно новое.", true);
      if (document.body.dataset.view === "onboarding") setStep(2);
    } catch (err) {
      showMsg($("#confirm-msg"), err.message);
    } finally {
      busy(btn, false);
    }
  });

  /* ---------- фильтры ---------- */
  function renderTags() {
    const box = $("#tags"), input = $("#kw-input");
    box.querySelectorAll(".tag").forEach((t) => t.remove());
    state.keywords.forEach((k, i) => {
      box.insertBefore(h("span", { class: "tag" },
        k,
        h("button", { type: "button", "aria-label": `Убрать «${k}»`, onclick: () => { state.keywords.splice(i, 1); renderTags(); input.focus(); } }, "×")
      ), input);
    });
  }

  function addKeywords(raw) {
    raw.split(",").map((s) => s.trim()).filter(Boolean).forEach((k) => {
      if (state.keywords.length < 10 && !state.keywords.some((x) => x.toLowerCase() === k.toLowerCase())) state.keywords.push(k.slice(0, 80));
    });
    renderTags();
  }

  const kw = $("#kw-input");
  kw.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addKeywords(kw.value); kw.value = ""; }
    else if (e.key === "Backspace" && !kw.value && state.keywords.length) { state.keywords.pop(); renderTags(); }
  });
  kw.addEventListener("blur", () => { if (kw.value.trim()) { addKeywords(kw.value); kw.value = ""; } });
  $("#tags").addEventListener("click", (e) => { if (e.target === e.currentTarget) kw.focus(); });

  $("#filters-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("button[type=submit]", e.currentTarget), msg = $("#filters-msg");
    if (kw.value.trim()) { addKeywords(kw.value); kw.value = ""; }
    showMsg(msg, "");
    let loc = segValue("loc");
    if (loc === "Другой город") loc = $("#loc-custom").value.trim();
    if (!loc) return showMsg(msg, "Введите название города.");
    busy(btn, true);
    try {
      applyProfile(await api("/api/filters", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keywords: state.keywords, location: loc, employment_type: segValue("emp") }),
      }), true);
      if (document.body.dataset.view === "onboarding") { setView("app"); runSearch(); }
    } catch (err) { showMsg(msg, err.message); }
    finally { busy(btn, false); }
  });

  $("#resume-next").addEventListener("click", () => setStep(3));
  $("#filters-back").addEventListener("click", () => setStep(2));

  /* ---------- поиск ---------- */
  const cards = $("#cards");

  function skeleton() {
    return h("div", { class: "skel", "aria-hidden": "true" }, h("i"), h("div", {}, h("i"), h("i"), h("i")));
  }
  const dropSkeleton = () => { const s = $(".skel", cards); if (s) s.remove(); };

  function insertSorted(el, score) {
    const before = [...cards.children].find((c) => c.classList.contains("skel") || Number(c.dataset.score) < score);
    cards.insertBefore(el, before || null);
  }

  function buildCard(c) {
    const missing = c.missing_skills;
    const letterBox = h("div", { class: "letter" }, h("div", { class: "letter__in" }));
    const letterBtn = h("button", { class: "btn btn--primary btn--sm", type: "button", "aria-expanded": "false" }, "Письмо");
    let loaded = false;

    letterBtn.addEventListener("click", async () => {
      const open = !letterBox.classList.contains("open");
      if (open && !loaded) {
        busy(letterBtn, true);
        try {
          const l = await api(`/api/letter/${c.id}`);
          const pre = h("pre", { text: l.text });
          const copy = h("button", { class: "btn btn--ghost btn--sm", type: "button" }, "Скопировать письмо");
          copy.addEventListener("click", async () => {
            try { await navigator.clipboard.writeText(l.text); }
            catch { const r = document.createRange(); r.selectNodeContents(pre); const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand("copy"); s.removeAllRanges(); }
            copy.textContent = "Скопировано";
            setTimeout(() => (copy.textContent = "Скопировать письмо"), 1800);
          });
          $(".letter__in", letterBox).append(h("div", { class: "letter__box" }, pre, copy));
          loaded = true;
        } catch (e) { toast(e.message); return; }
        finally { busy(letterBtn, false); }
      }
      letterBox.classList.toggle("open", open);
      letterBtn.setAttribute("aria-expanded", String(open));
      letterBtn.textContent = open ? "Скрыть письмо" : "Письмо";
    });

    return h("article", { class: "card", "data-score": c.match_score },
      h("div", { class: "card__top" },
        makeDial(c.match_score),
        h("div", { class: "card__title" },
          h("h3", {}, h("a", { href: `/?vacancy=${encodeURIComponent(c.id)}`, "data-vacancy": c.id, text: c.title })),
          h("span", { class: "muted", text: c.company || "Компания не указана" }),
          h("span", { class: "verdict", "data-tone": tone(c.match_score), text: verdict(c.match_score) }))),
      missing.length
        ? h("div", {}, h("div", { class: "card__label", text: "Не хватает в резюме" }),
            h("ul", { class: "chips" }, missing.map((m) => h("li", { class: "chip chip--miss", text: m }))))
        : h("p", { class: "card__ok", text: "Ключевые требования закрыты" }),
      h("div", { class: "card__actions" },
          h("a", { class: "btn btn--ghost btn--sm", href: `/?vacancy=${encodeURIComponent(c.id)}`, "data-vacancy": c.id }, "Полное описание"),
        letterBtn,
        c.url ? h("a", { class: "btn btn--ghost btn--sm", href: c.url, target: "_blank", rel: "noopener" }, "Открыть на hh.ru") : null),
      letterBox);
  }

  function buildFeaturedCard(c) {
    const preview = (c.summary || "").trim();
    return h("article", { class: "featured-card" },
        c.is_demo ? h("span", { class: "demo-label", text: "ДЕМО — НЕ РЕАЛЬНАЯ ВАКАНСИЯ" }) : null,
        h("h3", {}, h("a", { href: `/?vacancy=${encodeURIComponent(c.id)}`, "data-vacancy": c.id, text: c.title })),
        h("span", { class: "muted", text: c.company || "Компания не указана" }),
        preview ? h("p", { class: "muted", text: `${preview.slice(0, 220)}${preview.length > 220 ? "…" : ""}` }) : null,
        c.skills.length ? h("ul", { class: "chips" }, c.skills.slice(0, 5).map((skill) => h("li", { class: "chip", text: skill }))) : null,
        c.is_demo ? h("p", { class: "demo-warning", text: "Учебный пример, размещённой вакансии и отклика здесь нет." }) : null,
        h("a", { class: "btn btn--ghost btn--sm", href: `/?vacancy=${encodeURIComponent(c.id)}`, "data-vacancy": c.id }, "Подробности и условия"));
  }

  function salaryText(salary) {
    if (!salary) return "Не указана";
    const amount = (value) => value == null ? "" : nf.format(value);
    const range = salary.from != null && salary.to != null
        ? `${amount(salary.from)}–${amount(salary.to)}`
        : salary.from != null ? `от ${amount(salary.from)}`
          : salary.to != null ? `до ${amount(salary.to)}` : "";
    if (!range) return "Не указана";
    return `${range} ${salary.currency || ""}${salary.gross == null ? "" : salary.gross ? " до вычета налогов" : " на руки"}`.trim();
  }

  async function showVacancy(id) {
    if (document.body.dataset.view !== "vacancy") state.returnView = document.body.dataset.view;
    setView("vacancy");
    const detail = $("#vacancy-detail");
    detail.replaceChildren(h("p", { class: "muted", text: "Загружаем описание с hh.ru…" }));
    try {
        const v = await api(`/api/vacancy/${encodeURIComponent(id)}`);
        const published = v.published_at ? new Date(v.published_at) : null;
        const publishedText = published && !Number.isNaN(published.getTime())
          ? published.toLocaleDateString("ru-RU") : "Не указана";
        const meta = [
          ["Компания", v.company],
          ["Местоположение", v.location],
          ["Зарплата", salaryText(v.salary)],
          ["Опыт работы", v.experience],
          ["Занятость", v.employment],
          ["График", v.schedule],
          ["Опубликована", publishedText],
          ["Специализация", (v.professional_roles || []).join(", ")],
        ].filter(([, value]) => value);
        detail.replaceChildren(
          h("header", { class: "vacancy-detail__head" },
            h("h1", { id: "vacancy-title", text: v.title }),
            h("span", { class: "muted", text: v.is_demo
              ? "Демонстрационная карточка. Это не опубликованная вакансия."
              : "Актуальность и первоисточник — hh.ru" })),
          v.is_demo ? h("p", { class: "demo-warning", text: "Это оригинальный учебный пример, он не представляет работодателя и не принимает отклики." }) : null,
          v.notice ? h("p", { class: "vacancy-notice", text: v.notice }) : null,
          h("dl", { class: "vacancy-detail__meta" },
            meta.map(([label, value]) => h("div", {}, h("dt", { text: label }), h("dd", { text: value })))),
          (v.skills || []).length ? h("section", { class: "vacancy-detail__section" },
            h("h2", { text: "Ключевые навыки" }),
            h("ul", { class: "chips vacancy-detail__skills" }, v.skills.map((skill) => h("li", { class: "chip", text: skill })))) : null,
          h("section", { class: "vacancy-detail__section" },
            h("h2", { text: "Полное описание" }),
            h("p", { class: "vacancy-detail__description", text: v.description || "Работодатель не добавил описание." })),
          v.url ? h("a", { class: "btn btn--primary", href: v.url, target: "_blank", rel: "noopener" }, "Откликнуться на hh.ru") : null);
    } catch (e) {
      detail.replaceChildren(
        h("h1", { id: "vacancy-title", text: "Не удалось открыть вакансию" }),
        h("p", { class: "msg", role: "alert", text: e.message }));
    }
  }

  function handleEvent(name, d, stats) {
    const status = $("#search-status");
    if (name === "stage") {
      status.textContent = d.text;
    } else if (name === "plan") {
      stats.total = d.analyzing + d.cached;
      if (d.analyzing) {
        status.textContent = `Анализирую вакансий: ${d.analyzing}. Карточки появляются по мере готовности.`;
        for (let i = 0; i < d.analyzing; i++) cards.append(skeleton());
      } else if (d.cached) {
        status.textContent = "Новых вакансий нет. Показываю ранее проанализированные.";
      }
      $("#empty").hidden = true;
    } else if (name === "card") {
      dropSkeleton();
      insertSorted(buildCard(d), d.match_score);
      stats.shown++;
    } else if (name === "failed") {
      dropSkeleton();
      stats.failed++;
    } else if (name === "error") {
      const error = new Error(d.message);
      error.code = d.code;
      throw error;
    } else if (name === "done") {
      cards.querySelectorAll(".skel").forEach((s) => s.remove());
      if (d.shown) status.textContent = `Готово. Показано вакансий: ${d.shown}${d.failed ? `, не удалось проанализировать: ${d.failed}` : ""}.`;
      else if (d.failed) status.textContent = "ИИ сейчас недоступен. Попробуйте чуть позже.";
      else if (d.rejected) status.textContent = `Нашли вакансий: ${d.rejected}, но ни одна не подходит под ваше резюме. Измените должности.`;
      else status.textContent = "Ничего не нашли. Измените должности или город.";
      if (!d.shown) {
        $("#empty").hidden = false;
        stats.needDemo = d.failed
          ? "Не удалось проанализировать вакансии: ИИ сейчас недоступен."
          : d.rejected
            ? "Среди найденных вакансий нет подходящих под ваше резюме."
            : "По вашим должностям и городу ничего не нашли.";
      }
    }
  }

  /* Запасной вариант: если реальный поиск ничего не дал, показываем демо-вакансии. */
  async function showDemoFallback(reason) {
    const status = $("#search-status");
    try {
      const result = await api("/api/vacancies/demo");
      if (!result.items.length) return false;
      cards.replaceChildren(...result.items.map(buildFeaturedCard));
      $("#empty").hidden = true;
      status.textContent = `${reason} ${result.notice}`.trim();
      return true;
    } catch (err) {
      status.textContent = `${reason} Не удалось загрузить и демо-вакансии: ${err.message}`.trim();
      return false;
    }
  }

  async function runSearch() {
    const btn = $("#search-btn"), status = $("#search-status");
    state.searching = true;
    btn.disabled = true;
    btn.classList.add("is-busy");
    cards.replaceChildren();
    $("#empty").hidden = true;
    $("#progress").hidden = false;
    status.textContent = "Сначала ИИ читает ваше резюме…";
    $(".results__bar").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    const stats = { shown: 0, failed: 0, total: 0 };

    try {
      const res = await fetch("/api/search", { credentials: "same-origin", headers: { Accept: "text/event-stream" } });
      if (res.status === 401) { onSessionExpired(); throw new Error("Сессия истекла. Войдите снова."); }
      if (!res.ok) {
        let d = ""; try { d = (await res.json()).detail; } catch { /* */ }
        throw new Error(typeof d === "string" && d ? d : "Не удалось запустить поиск.");
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) !== -1) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          let ev = "message", data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) ev = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (data) handleEvent(ev, JSON.parse(data), stats);
        }
      }
      if (stats.needDemo) await showDemoFallback(stats.needDemo);
    } catch (e) {
      cards.querySelectorAll(".skel").forEach((s) => s.remove());
      status.textContent = e.message;
      let showedFallback = false;
      if (e.status !== 401 && !/Сначала (добавьте|задайте)|Сессия истекла|ещё выполняется/i.test(e.message)) {
        showedFallback = await showDemoFallback(`${e.message}`);
      }
      if (!showedFallback && !cards.children.length) $("#empty").hidden = false;
      toast(status.textContent);
    } finally {
      state.searching = false;
      btn.classList.remove("is-busy");
      $("#progress").hidden = true;
      updateSearchButton(true);
    }
  }
  $("#search-btn").addEventListener("click", runSearch);

  /* ---------- экраны и шаги ---------- */
  function setView(v) {
    document.body.dataset.view = v;
    $("#vacancy-page").hidden = v !== "vacancy";
    window.scrollTo({ top: 0 });
  }

  function setStep(n) {
    document.body.dataset.step = String(n);
    $("#step-2").className = n === 2 ? "current" : "done";
    $("#step-3").className = n === 3 ? "current" : "";
    $("#filters-submit").textContent = "Сохранить и найти вакансии";
    window.scrollTo({ top: 0 });
  }

  function enter(p) {
    state.account = p.account;
    $("#user-name").textContent = p.account.login || p.account.name;
    $("#user-box").hidden = false;
    applyProfile(p);
    if (p.onboarded) { $("#filters-submit").textContent = "Сохранить фильтры"; setView("app"); }
    else { setView("onboarding"); setStep(p.resume.has ? 3 : 2); }
  }

  function resetSession() {
    state.account = null; state.profile = null; state.keywords = [];
    chatState.loaded = false; chatLog.replaceChildren(); closeDialog(chatDialog);
    cards.replaceChildren();
    $("#empty").hidden = false;
    $("#progress").hidden = true;
    $("#user-box").hidden = true;
    $("#resume-text").value = "";
    showMsg($("#resume-msg"), ""); showMsg($("#filters-msg"), "");
  }

  function onSessionExpired() {
    if (document.body.dataset.view === "auth") return;
    resetSession(); setView("auth"); toast("Сессия истекла. Войдите снова.");
  }

  $("#logout").addEventListener("click", async () => {
    try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch { /* всё равно выходим */ }
    resetSession(); setView("auth");
  });

  /* ---------- регистрация и вход ---------- */
  let authMode = "register";
  const atabs = [$("#atab-register"), $("#atab-login")];
  function setAuthMode(mode) {
    authMode = mode;
    const reg = mode === "register";
    atabs.forEach((t, i) => { t.setAttribute("aria-selected", String(reg === (i === 0))); t.tabIndex = reg === (i === 0) ? 0 : -1; });
    $("#name-field").hidden = !reg;
    $("#login-hint").hidden = !reg;
    $("#a-pass").autocomplete = reg ? "new-password" : "current-password";
    $("#pass-hint").hidden = !reg;
    $("#auth-submit").textContent = reg ? "Создать аккаунт" : "Войти";
    showMsg($("#auth-msg"), "");
  }
  atabs.forEach((t, i) => {
    t.addEventListener("click", () => setAuthMode(i === 0 ? "register" : "login"));
    t.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") { setAuthMode(i === 0 ? "login" : "register"); atabs[1 - i].focus(); }
    });
  });

  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#auth-msg"), btn = $("#auth-submit");
    const login = $("#a-login").value.trim(), name = $("#a-name").value.trim();
    const email = $("#a-email").value.trim(), password = $("#a-pass").value;
    showMsg(msg, "");
    if (!login) return showMsg(msg, authMode === "register" ? "Придумайте логин." : "Введите логин.");
    if (authMode === "register" && !/^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$/.test(login)) {
      return showMsg(msg, "Логин: 3–32 символа, латинские буквы, цифры и знаки _ . -, начинается с буквы или цифры.");
    }
    if (!password) return showMsg(msg, "Введите пароль.");
    if (authMode === "register" && password.length < 8) return showMsg(msg, "Пароль должен быть не короче 8 символов.");
    if (!email) return showMsg(msg, "Введите электронную почту.");
    busy(btn, true);
    try {
      const p = await api(`/api/auth/${authMode}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(authMode === "register" ? { login, name, email, password } : { login, email, password }),
      });
      $("#a-pass").value = "";
      enter(p);
    } catch (err) { showMsg(msg, err.message); }
    finally { busy(btn, false); }
  });

  /* ---------- чат с ИИ (только о работе) ---------- */
  const chatDialog = $("#chat-dialog");
  const chatLog = $("#chat-log"), chatInput = $("#chat-input"), chatSend = $("#chat-send");
  const chatState = { loaded: false, sending: false };
  const CHAT_INTRO = "Здравствуйте! Спросите о работе и карьере: какие вакансии вам подходят, где выше зарплаты, какие навыки добавить, как пройти собеседование. Я учитываю ваше резюме.";
  const CHAT_SUGGESTIONS = [
    "Какие работы с большой зарплатой мне подойдут?",
    "Какие навыки добавить, чтобы вырасти в должности?",
    "Что спросят на собеседовании на мою позицию?",
  ];

  function chatScroll() { chatLog.scrollTop = chatLog.scrollHeight; }

  function buildChatCard(c) {
    const meta = [c.company, c.salary ? `💰 ${c.salary}` : ""].filter(Boolean);
    return h("article", { class: "chat-card" },
      c.is_demo ? h("span", { class: "demo-label", text: "ДЕМО — НЕ РЕАЛЬНАЯ ВАКАНСИЯ" }) : null,
      h("h3", {}, h("a", { href: `/?vacancy=${encodeURIComponent(c.id)}`, "data-vacancy": c.id, text: c.title })),
      meta.length ? h("div", { class: "chat-card__meta" }, meta.map((m) => h("span", { text: m }))) : null,
      c.match_score != null ? h("span", { class: "chip chip--ok", text: `Совпадение с резюме: ${c.match_score}%` }) : null,
      (c.skills || []).length ? h("ul", { class: "chips" }, c.skills.map((x) => h("li", { class: "chip", text: x }))) : null);
  }

  function addChatMessage(role, text, extra = {}) {
    const cls = role === "user" ? "chat__msg chat__msg--user" : `chat__msg chat__msg--ai${extra.off ? " chat__msg--off" : ""}`;
    chatLog.append(h("div", { class: cls, text }));
    if (extra.notice) chatLog.append(h("div", { class: "chat__notice", text: extra.notice }));
    if ((extra.vacancies || []).length) chatLog.append(h("div", { class: "chat__cards" }, extra.vacancies.map(buildChatCard)));
    chatScroll();
  }

  function renderChatHistory(messages) {
    chatLog.replaceChildren();
    addChatMessage("ai", CHAT_INTRO);
    for (const m of messages) addChatMessage(m.role === "user" ? "user" : "ai", m.text, { notice: m.notice, vacancies: m.vacancies });
  }

  function updateChatAvailability() {
    const ready = !!(state.profile && state.profile.resume.has);
    chatInput.disabled = !ready || chatState.sending;
    chatSend.disabled = !ready || chatState.sending;
    chatInput.placeholder = ready ? "Например: какие работы с большой зарплатой мне подойдут?" : "Сначала добавьте резюме: ИИ отвечает с учётом него";
    $("#chat-chips").replaceChildren(...(ready ? CHAT_SUGGESTIONS : []).map((t) =>
      h("button", { type: "button", text: t, onclick: () => sendChat(t) })));
  }

  async function openChat() {
    showMsg($("#chat-msg"), "");
    openDialog(chatDialog);
    updateChatAvailability();
    if (!chatState.loaded) {
      renderChatHistory([]);
      try {
        const r = await api("/api/chat");
        renderChatHistory(r.messages);
        chatState.loaded = true;
      } catch (e) { showMsg($("#chat-msg"), e.message); }
    }
    if (!chatInput.disabled) chatInput.focus();
  }

  async function sendChat(text) {
    text = (text ?? chatInput.value).trim();
    const msg = $("#chat-msg");
    showMsg(msg, "");
    if (!text || chatState.sending) return;
    if (text.length > 500) return showMsg(msg, "Слишком длинное сообщение: не больше 500 символов.");
    chatState.sending = true;
    updateChatAvailability();
    addChatMessage("user", text);
    chatInput.value = ""; chatInput.style.height = "";
    const wait = h("div", { class: "chat__msg chat__msg--ai chat__msg--wait", text: "ИИ читает резюме и думает…" });
    chatLog.append(wait); chatScroll();
    try {
      const r = await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text }) });
      wait.remove();
      addChatMessage("ai", r.reply, { off: r.off_topic, notice: r.notice, vacancies: r.vacancies });
    } catch (e) {
      wait.remove();
      showMsg(msg, e.message);
      if (!chatInput.value) chatInput.value = text;
    } finally {
      chatState.sending = false;
      updateChatAvailability();
      if (!chatInput.disabled) chatInput.focus();
    }
  }

  $("#chat-open").addEventListener("click", openChat);
  $("#chat-close").addEventListener("click", () => closeDialog(chatDialog));
  chatDialog.addEventListener("click", (e) => { if (e.target === chatDialog) closeDialog(chatDialog); });
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat(); });
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
  });
  chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 120)}px`;
  });
  $("#chat-clear").addEventListener("click", async () => {
    showMsg($("#chat-msg"), "");
    try { await api("/api/chat", { method: "DELETE" }); renderChatHistory([]); }
    catch (e) { showMsg($("#chat-msg"), e.message); }
  });

  /* ---------- переход к вакансии без перезагрузки (список результатов остаётся) ---------- */
  function leaveVacancy() {
    const v = state.returnView && state.returnView !== "vacancy" ? state.returnView : (state.account ? "app" : "auth");
    setView(v);
  }

  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = e.target.closest("a[data-vacancy]");
    if (link) {
      e.preventDefault();
      const dlg = link.closest("dialog");
      if (dlg) closeDialog(dlg);
      history.pushState({ vacancy: link.dataset.vacancy }, "", `/?vacancy=${encodeURIComponent(link.dataset.vacancy)}`);
      showVacancy(link.dataset.vacancy);
      return;
    }
    if (e.target.closest("#vacancy-back")) {
      e.preventDefault();
      history.pushState({}, "", "/");
      leaveVacancy();
    }
  });

  window.addEventListener("popstate", () => {
    const id = new URLSearchParams(location.search).get("vacancy");
    if (id) showVacancy(id); else if (document.body.dataset.view === "vacancy") leaveVacancy();
  });

  /* ---------- синхронизация с ботом: подтягиваем свежие данные аккаунта ---------- */
  let lastSync = 0;
  async function syncFromServer() {
    const view = document.body.dataset.view;
    if (!state.account || !state.profile || state.searching || (view !== "app" && view !== "onboarding")) return;
    if (Date.now() - lastSync < 3000) return;
    lastSync = Date.now();
    let p;
    try { p = await api("/api/me"); } catch { return; }
    const prev = state.profile;
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    if (same(p.resume, prev.resume) && same(p.filters, prev.filters)) return;
    if (view === "onboarding" && p.onboarded) { enter(p); return; }  // настройка закончена в боте

    const filtersForm = $("#filters-submit").closest("form");
    const editing = same(state.keywords, prev.filters.keywords) === false || (filtersForm && filtersForm.contains(document.activeElement));
    state.profile = p;
    if (!same(p.resume, prev.resume)) { renderResume(p.resume); $("#resume-next").disabled = !p.resume.has; updateChatAvailability(); }
    if (!same(p.filters, prev.filters) && !editing) renderFilters(p.filters, p.options);
    updateSearchButton();
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncFromServer(); });
  window.addEventListener("focus", syncFromServer);
  setInterval(() => { if (!document.hidden) syncFromServer(); }, 20000);

  /* ---------- старт ---------- */
  const initialVacancyId = new URLSearchParams(location.search).get("vacancy");
  api("/api/me")
    .then((profile) => {
      enter(profile);
      if (initialVacancyId) showVacancy(initialVacancyId);
    })
    .catch((e) => {
      if (e.status === 401) return setView("auth");
      setView("auth");
      showMsg($("#auth-msg"), "Не удаётся связаться с сервером. Обновите страницу.");
    })
    .finally(() => {
      if (initialVacancyId && document.body.dataset.view === "auth") showVacancy(initialVacancyId);
    });
})();
