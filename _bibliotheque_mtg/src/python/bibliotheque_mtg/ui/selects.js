// Keep the existing <select> elements as the source of truth for forms and app.js.
(() => {
  let opened = null;
  let serial = 0;

  function closeMenu(restoreFocus = false) {
    if (!opened) return;
    const {button, menu} = opened;
    opened = null;
    button.setAttribute("aria-expanded", "false");
    button.removeAttribute("aria-activedescendant");
    menu.remove();
    if (restoreFocus) button.focus();
  }

  function enhance(select) {
    if (select.dataset.customSelect || select.dataset.multiFilter || select.multiple || select.size > 1) return;
    select.dataset.customSelect = "true";
    const wrapper = document.createElement("span");
    wrapper.className = "custom-select";
    select.before(wrapper);
    wrapper.append(select);
    select.classList.add("custom-select-native");
    select.tabIndex = -1;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "custom-select-button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    const label = select.getAttribute("aria-label") ||
      select.labels?.[0]?.querySelector(".search-field-label")?.textContent?.trim() ||
      [...(select.labels?.[0]?.childNodes || [])].filter(node => node !== wrapper && node.nodeType === Node.TEXT_NODE)
        .map(node => node.textContent.trim()).filter(Boolean).join(" ") || "Choisir une option";
    button.setAttribute("aria-label", label);
    wrapper.append(button);

    function sync() {
      button.replaceChildren();
      appendEditionOptionLabel(button, select.selectedOptions[0] || select.options[0]);
      button.setAttribute("aria-label", `${label} : ${button.textContent}`);
      button.disabled = select.disabled;
    }

    function openMenu() {
      if (select.disabled || !select.options.length) return;
      closeMenu();
      sync();
      const menu = document.createElement("div");
      menu.className = "custom-select-menu";
      menu.id = `custom-select-menu-${++serial}`;
      menu.setAttribute("role", "listbox");
      menu.setAttribute("aria-label", label);
      const items = [];
      for (const [index, option] of [...select.options].entries()) {
        const item = document.createElement("div");
        item.className = "custom-select-option";
        item.dataset.optionValue = option.value;
        item.id = `${menu.id}-option-${index}`;
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(option.selected));
        item.setAttribute("aria-disabled", String(option.disabled));
        appendEditionOptionLabel(item, option);
        item.addEventListener("click", () => choose(index));
        menu.append(item);
        items.push(item);
      }
      (select.closest("dialog[open]") || document.body).append(menu);
      const rect = button.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom - 8;
      const spaceAbove = rect.top - 8;
      const height = Math.min(menu.scrollHeight, 352, Math.max(spaceBelow, spaceAbove));
      const upward = spaceBelow < Math.min(height, 180) && spaceAbove > spaceBelow;
      menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - rect.width - 8))}px`;
      menu.style.width = `${Math.min(Math.max(rect.width, 180), window.innerWidth - 16)}px`;
      menu.style.maxHeight = `${Math.max(80, height)}px`;
      menu.style.top = `${upward ? Math.max(8, rect.top - Math.max(80, height) - 4) : rect.bottom + 4}px`;
      button.setAttribute("aria-controls", menu.id);
      button.setAttribute("aria-expanded", "true");
      opened = {select, button, menu, items, active: Math.max(0, select.selectedIndex)};
      setActive(opened.active);
    }

    function setActive(index) {
      if (!opened || opened.select !== select) return;
      const next = Math.max(0, Math.min(index, opened.items.length - 1));
      if (select.options[next]?.disabled) return;
      opened.items[opened.active]?.classList.remove("active");
      opened.active = next;
      opened.items[next]?.classList.add("active");
      button.setAttribute("aria-activedescendant", opened.items[next].id);
      const item = opened.items[next];
      const itemRect = item.getBoundingClientRect();
      const menuRect = opened.menu.getBoundingClientRect();
      if (itemRect.top < menuRect.top) opened.menu.scrollTop -= menuRect.top - itemRect.top;
      else if (itemRect.bottom > menuRect.bottom) opened.menu.scrollTop += itemRect.bottom - menuRect.bottom;
    }

    function choose(index) {
      if (select.options[index]?.disabled) return;
      const changed = select.selectedIndex !== index;
      select.selectedIndex = index;
      closeMenu(true);
      sync();
      if (changed) select.dispatchEvent(new Event("change", {bubbles: true}));
    }

    button.addEventListener("click", event => {
      event.preventDefault();
      if (opened?.select === select) closeMenu();
      else openMenu();
    });
    select.labels?.[0]?.addEventListener("click", event => {
      if (event.composedPath().includes(wrapper)) return;
      event.preventDefault();
      button.click();
    });
    let search = "", searchTimer;
    button.addEventListener("keydown", event => {
      if (event.key === "Escape" && opened?.select === select) { event.preventDefault(); closeMenu(true); return; }
      if (event.key.length === 1 && event.key !== " " && !event.altKey && !event.ctrlKey && !event.metaKey) {
        event.preventDefault();
        if (!opened || opened.select !== select) openMenu();
        search += event.key.toLocaleLowerCase();
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { search = ""; }, 700);
        const match = [...select.options].findIndex(option => !option.disabled && option.textContent.trim().toLocaleLowerCase().startsWith(search));
        if (match >= 0) setActive(match);
        return;
      }
      if (!["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)) return;
      event.preventDefault();
      if (!opened || opened.select !== select) { openMenu(); return; }
      if (event.key === "Enter" || event.key === " ") { choose(opened.active); return; }
      if (event.key === "Home") { setActive(0); return; }
      if (event.key === "End") { setActive(select.options.length - 1); return; }
      const step = event.key === "ArrowDown" ? 1 : -1;
      let next = opened.active + step;
      while (next >= 0 && next < select.options.length && select.options[next].disabled) next += step;
      if (next >= 0 && next < select.options.length) setActive(next);
    });
    select.addEventListener("change", sync);
    select.addEventListener("input", sync);
    new MutationObserver(() => {
      if (opened?.select !== select) { sync(); return; }
      if (select.disabled || !select.options.length) { closeMenu(); sync(); return; }
      const activeValue = opened.items[opened.active]?.dataset.optionValue;
      const scrollTop = opened.menu.scrollTop;
      openMenu();
      const index = [...select.options].findIndex(option => option.value === activeValue);
      if (index >= 0) {
        setActive(index);
        opened.menu.scrollTop = scrollTop;
      }
    }).observe(select, {childList: true, subtree: true, attributes: true, characterData: true});
    sync();
  }

  document.querySelectorAll("select").forEach(enhance);
  new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      if (node.matches("select")) enhance(node);
      node.querySelectorAll?.("select").forEach(enhance);
    }
  }).observe(document.body, {childList: true, subtree: true});
  document.addEventListener("pointerdown", event => {
    if (opened && !opened.menu.contains(event.target) && !opened.button.contains(event.target)) closeMenu();
  });
  document.addEventListener("scroll", event => {
    if (opened && event.target !== opened.menu) closeMenu();
  }, true);
  window.addEventListener("resize", () => closeMenu());
})();
