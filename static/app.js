    let state = { accounts: { accounts: {} }, campaign: { jobs: [], defaults: {} }, inventory: [], process: {}, assistantPresets: {}, activity: { items: [] }, ai: {}, autonomy: {} };
    let assistantReview = {};
    let customPhotos = [];
    let autoPhotoDraft = {};
    let autoPhotoDraftDirty = false;
    let dirty = false;
    const $ = (id) => document.getElementById(id);

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[character]));
    }

    function toast(message) {
      const el = $("toast");
      el.textContent = message;
      el.classList.remove("hidden");
      setTimeout(() => el.classList.add("hidden"), 2800);
    }

    function setDirty(value = true) {
      dirty = value;
      $("saveState").textContent = dirty ? "Cambios sin guardar" : "Guardado";
    }

    const DASHBOARD_TOKEN = window.DASHBOARD_TOKEN || "";

    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {
          "Content-Type": "application/json",
          "X-Marketplace-Token": DASHBOARD_TOKEN,
          ...(options.headers || {}),
        },
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || "Error");
      return payload;
    }

    function activeAccountKey() {
      return $("defaultAccount").value || Object.keys(state.accounts.accounts || {})[0] || "cuenta1";
    }

    function automaticProducts() {
      const inventory = (state.autonomy?.families || []).map(family => ({
        key: family.key,
        label: family.label,
        images: family.images || [],
      }));
      const custom = (state.autonomy?.custom_products || []).filter(product => product.active).map(product => ({
        key: product.job?.family_key || `custom:${product.id}`,
        label: product.title,
        images: product.images || [],
      }));
      return [...inventory, ...custom];
    }

    function resetAutoPhotoDraft() {
      const persisted = state.autonomy?.photo_assignments || {};
      const accounts = Object.keys(state.accounts?.accounts || {});
      autoPhotoDraft = {};
      accounts.forEach(account => {
        autoPhotoDraft[account] = {};
        automaticProducts().forEach(product => {
          const defined = persisted[account]?.[product.key];
          autoPhotoDraft[account][product.key] = (defined?.length ? defined : product.images).map(image => image.id);
        });
      });
      autoPhotoDraftDirty = false;
    }

    function renderAutoPhotoAssignments() {
      const accounts = state.accounts?.accounts || {};
      const selectedAccounts = [...$("autoAccounts").querySelectorAll("input:checked")].map(input => input.value);
      const products = automaticProducts();
      if (!selectedAccounts.length || !products.length) {
        $("autoPhotoAssignments").innerHTML = `<div class="review-empty">Selecciona una cuenta activa y asegúrate de que haya productos con fotos.</div>`;
        return;
      }
      selectedAccounts.forEach(account => {
        autoPhotoDraft[account] ||= {};
        products.forEach(product => {
          autoPhotoDraft[account][product.key] ||= product.images.map(image => image.id);
        });
      });
      $("autoPhotoAssignments").innerHTML = products.map(product => `<article class="photo-assignment-product">
        <h4>${escapeHtml(product.label)}</h4>
        <div class="photo-account-grid">${selectedAccounts.map(account => {
          const selected = new Set(autoPhotoDraft[account]?.[product.key] || []);
          const choices = product.images.map((image, index) => `<label class="photo-choice" title="${escapeHtml(image.name)}">
            <img src="${escapeHtml(image.url)}" alt="${escapeHtml(product.label)} - foto ${index + 1}">
            <input type="checkbox" data-auto-photo-account="${escapeHtml(account)}" data-auto-photo-family="${escapeHtml(product.key)}" value="${escapeHtml(image.id)}" ${selected.has(image.id) ? "checked" : ""}>
          </label>`).join("");
          return `<div class="photo-account"><strong>${escapeHtml(accounts[account]?.display_name || account)}</strong>
            <div class="photo-picker">${choices || '<span class="muted">Sin fotos disponibles</span>'}</div>
            <span class="photo-count">${selected.size} fotos seleccionadas</span>
          </div>`;
        }).join("")}</div>
      </article>`).join("");
      document.querySelectorAll("[data-auto-photo-account]").forEach(input => {
        input.onchange = () => {
          const account = input.dataset.autoPhotoAccount;
          const family = input.dataset.autoPhotoFamily;
          const selected = new Set(autoPhotoDraft[account]?.[family] || []);
          if (input.checked && selected.size >= 10) {
            input.checked = false;
            toast("Solo se pueden usar hasta 10 fotos por publicación");
            return;
          }
          input.checked ? selected.add(input.value) : selected.delete(input.value);
          autoPhotoDraft[account][family] = [...selected];
          autoPhotoDraftDirty = true;
          setDirty();
          renderAutoPhotoAssignments();
        };
      });
    }

    function renderOverview() {
      const items = [...(state.activity?.items || [])].reverse();
      const counts = status => items.filter(item => item.status === status).length;
      $("activityPending").textContent = counts("pending");
      $("activityRunning").textContent = counts("running");
      $("activityPrepared").textContent = items.filter(item => ["planned", "tested", "prepared"].includes(item.status)).length;
      $("activityPublished").textContent = counts("published");
      $("activityFailed").textContent = items.filter(item => ["failed", "stopped"].includes(item.status)).length;
      $("activityEmpty").classList.toggle("hidden", items.length > 0);
      $("activityTableWrap").classList.toggle("hidden", items.length === 0);
      const statusLabels = {
        pending: "Pendiente",
        running: "En proceso",
        published: "Publicada",
        failed: "Fallida",
        tested: "Prueba lista",
        prepared: "Preparada",
        planned: "Plan revisado",
        skipped: "Omitida",
        stopped: "Detenida",
      };
      $("activityTable").innerHTML = items.slice(0, 100).map(item => {
        const date = item.updated_at ? new Date(item.updated_at) : null;
        const time = date && !Number.isNaN(date.getTime())
          ? date.toLocaleString("es-NI", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
          : "";
        const source = String(item.description_source || "").startsWith("openai:")
          ? " · descripción IA"
          : item.description_source ? " · redacción local" : "";
        return `<tr>
          <td>${escapeHtml(time)}</td>
          <td><strong>${escapeHtml(item.name)}</strong><br><span class="muted">${escapeHtml(source.replace(" · ", ""))}</span></td>
          <td><span class="status-tag status-${escapeHtml(item.status)}">${escapeHtml(statusLabels[item.status] || item.status)}</span></td>
          <td>${escapeHtml(item.account)}</td>
          <td>${escapeHtml(item.detail || "")}</td>
        </tr>`;
      }).join("");
    }

    function renderAutonomy() {
      const autonomy = state.autonomy || {};
      const config = autonomy.config || {};
      const accounts = state.accounts.accounts || {};
      $("autoEnabled").checked = Boolean(config.enabled);
      $("autoMode").value = config.mode || "supervised";
      $("autoAccountStrategy").value = config.account_strategy || "round_robin";
      const selectedAccounts = new Set(config.accounts || [config.account]);
      $("autoAccounts").innerHTML = Object.keys(accounts).map(key => `<label class="weekday-choice"><input type="checkbox" value="${escapeHtml(key)}" ${selectedAccounts.has(key) ? "checked" : ""}> ${escapeHtml(accounts[key].display_name || key)}</label>`).join("");
      $("autoAccounts").querySelectorAll("input").forEach(input => input.onchange = () => {
        autoPhotoDraftDirty = true;
        setDirty();
        renderAutoPhotoAssignments();
      });
      $("autoStart").value = config.start_time || "09:00";
      $("autoEnd").value = config.end_time || "18:00";
      $("autoMaxDaily").value = config.max_per_day ?? 3;
      $("autoInterval").value = config.interval_minutes ?? 180;
      $("autoCooldown").value = config.cooldown_days ?? 3;
      $("autoJitter").value = config.slot_jitter_minutes ?? 7;

      const dayNames = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
      const activeDays = new Set(config.active_days || []);
      $("autoDays").innerHTML = dayNames.map((name, index) => `<label class="weekday-choice"><input type="checkbox" value="${index}" ${activeDays.has(index) ? "checked" : ""}> ${name}</label>`).join("");

      const selectedFamilies = new Set(config.selected_families || []);
      $("autoFamilies").innerHTML = (autonomy.families || []).map(family => `<label class="family-choice" title="${escapeHtml(family.reason || "")}">
        <input type="checkbox" value="${escapeHtml(family.key)}" ${selectedFamilies.has(family.key) ? "checked" : ""} ${family.eligible ? "" : "disabled"}>
        ${escapeHtml(family.label)} <span class="muted">${family.valid_variant_count}/${family.variant_count}</span>
      </label>`).join("");

      const counts = autonomy.summary?.counts || {};
      $("autoPublishedToday").textContent = autonomy.summary?.published_today || 0;
      $("autoPending").textContent = (counts.planned || 0) + (counts.queued || 0) + (counts.retry || 0);
      $("autoBlocked").textContent = counts.blocked || 0;
      $("autoWorker").textContent = autonomy.worker?.process_running ? "Activo" : "Detenido";

      renderAutonomyCalendar();
      renderAutonomyAlerts();
      renderAutonomyReport();
      renderCustomProducts();
      renderAutoPhotoAssignments();
    }

    function renderAutonomyCalendar() {
      const items = (state.autonomy?.queue || []).filter(item => !["cancelled", "skipped"].includes(item.status));
      const byDay = {};
      items.forEach(item => {
        const key = String(item.scheduled_at || "").slice(0, 10);
        (byDay[key] ||= []).push(item);
      });
      const dates = [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      for (let index = 0; index < 7; index += 1) {
        const day = new Date(today);
        day.setDate(today.getDate() + index);
        dates.push(day);
      }
      const labels = { planned: "Por aprobar", queued: "Programada", running: "En proceso", retry: "Reintento", published: "Publicada", tested: "Probada", blocked: "Bloqueada" };
      $("autonomyCalendar").innerHTML = dates.map(day => {
        const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
        const dayItems = byDay[key] || [];
        return `<div class="calendar-day"><h4>${escapeHtml(day.toLocaleDateString("es-NI", { weekday: "short", day: "numeric", month: "short" }))}</h4>
          ${dayItems.length ? dayItems.map(item => {
            const value = String(item.scheduled_at || "").slice(0, 16);
            const canCancel = ["planned", "queued", "retry", "blocked"].includes(item.status);
            return `<div class="calendar-item">
              <span>${escapeHtml(value.slice(11))} · ${escapeHtml(labels[item.status] || item.status)}</span>
              <strong>${escapeHtml(item.name)}</strong>
              <span>${escapeHtml(item.account)}</span>
              <input type="datetime-local" value="${escapeHtml(value)}" data-move-value="${escapeHtml(item.id)}">
              <div class="calendar-actions">
                <button data-auto-action="move" data-id="${escapeHtml(item.id)}" title="Cambiar hora">Mover</button>
                ${item.status === "planned" ? `<button data-auto-action="approve" data-id="${escapeHtml(item.id)}">Aprobar</button>` : ""}
                ${item.status === "blocked" ? `<button data-auto-action="retry" data-id="${escapeHtml(item.id)}">Reintentar</button>` : ""}
                ${canCancel ? `<button data-auto-action="cancel" data-id="${escapeHtml(item.id)}">Omitir</button>` : ""}
              </div>
            </div>`;
          }).join("") : `<span class="muted">Sin publicaciones</span>`}
        </div>`;
      }).join("");
      document.querySelectorAll("[data-auto-action]").forEach(button => {
        button.onclick = () => autonomyItemAction(button.dataset.id, button.dataset.autoAction);
      });
    }

    function renderAutonomyAlerts() {
      const alerts = state.autonomy?.alerts || [];
      $("autoAlerts").classList.toggle("review-empty", alerts.length === 0);
      $("autoAlerts").innerHTML = alerts.length ? alerts.map(alert => `<div class="alert-row">
        <div><strong>${escapeHtml(alert.code)}</strong><br><span>${escapeHtml(alert.message)}</span></div>
        <button data-alert-resolve="${escapeHtml(alert.id)}">Resolver</button>
      </div>`).join("") : "No hay alertas abiertas.";
      document.querySelectorAll("[data-alert-resolve]").forEach(button => {
        button.onclick = async () => {
          const result = await api("/api/autonomy/alert/resolve", { method: "POST", body: JSON.stringify({ id: button.dataset.alertResolve }) });
          state.autonomy = result.autonomy;
          renderAutonomy();
        };
      });
    }

    function renderAutonomyReport() {
      const report = state.autonomy?.report || {};
      const familyLabels = Object.fromEntries((state.autonomy?.families || []).map(item => [item.key, item.label]));
      const families = report.families || [];
      const errors = report.errors || [];
      $("autoReport").innerHTML = `<table>
        <thead><tr><th>Producto</th><th>Publicadas</th><th>Última publicación</th></tr></thead>
        <tbody>${families.length ? families.map(item => `<tr><td>${escapeHtml(familyLabels[item.family_key] || item.family_key || "Sin familia")}</td><td>${item.total}</td><td>${escapeHtml(item.last_at || "")}</td></tr>`).join("") : `<tr><td colspan="3" class="muted">Todavía no hay publicaciones automáticas registradas.</td></tr>`}</tbody>
      </table>${errors.length ? `<p class="muted">Incidencias: ${errors.map(item => `${escapeHtml(item.error_code)} (${item.total})`).join(" · ")}</p>` : ""}`;
    }

    function renderCustomPhotoPreview() {
      $("customPhotoPreview").innerHTML = customPhotos.length
        ? customPhotos.map(photo => `<img src="${escapeHtml(photo.url)}" alt="${escapeHtml(photo.name)}">`).join("")
        : `<span class="muted">Agrega entre 1 y 10 fotos reales del producto.</span>`;
    }

    async function uploadCustomPhotos(files) {
      if (!files.length) return;
      const selected = files.slice(0, Math.max(0, 10 - customPhotos.length));
      try {
        for (const file of selected) {
          if (file.size > 8 * 1024 * 1024) throw new Error(`${file.name} pesa más de 8 MB.`);
          const result = await api("/api/assistant/upload-image", {
            method: "POST",
            body: JSON.stringify({ filename: file.name, data: await dataUrlForFile(file) }),
          });
          customPhotos.push(result.media);
        }
        renderCustomPhotoPreview();
      } catch (error) {
        toast(error.message);
      }
    }

    async function saveCustomProduct() {
      const tags = $("customTags").value.split(/[,;]+/).map(value => value.trim()).filter(Boolean);
      const payload = {
        title: $("customTitle").value.trim(),
        price: Number($("customPrice").value || 0),
        description: $("customDescription").value.trim(),
        category: $("customCategory").value,
        condition: $("customCondition").value,
        location: $("customLocation").value.trim(),
        tags,
        image_ids: customPhotos.map(photo => photo.id),
        include_in_rotation: $("customRotation").checked,
        active: true,
      };
      const result = await api("/api/custom-product", { method: "POST", body: JSON.stringify(payload) });
      state.autonomy = result.autonomy;
      customPhotos = [];
      ["customTitle", "customPrice", "customDescription", "customTags"].forEach(id => $(id).value = "");
      $("customPhotos").value = "";
      renderAutonomy();
      toast("Producto agregado al catálogo automático");
    }

    function renderCustomProducts() {
      const products = state.autonomy?.custom_products || [];
      $("customProductsList").innerHTML = products.length ? products.map(product => `<div class="custom-product-row">
        <div><strong>${escapeHtml(product.title)}</strong><br><span class="muted">C$ ${escapeHtml(product.price)} · ${product.images?.length || 0} fotos${product.include_in_rotation ? " · en rotación" : ""}</span></div>
        <label><input type="checkbox" data-custom-active="${escapeHtml(product.id)}" ${product.active ? "checked" : ""}> Activo</label>
      </div>`).join("") : `<p class="muted">Todavía no has agregado productos manuales.</p>`;
      document.querySelectorAll("[data-custom-active]").forEach(input => {
        input.onchange = async () => {
          const result = await api("/api/custom-product/active", { method: "POST", body: JSON.stringify({ id: input.dataset.customActive, active: input.checked }) });
          state.autonomy = result.autonomy;
          renderAutonomy();
        };
      });
      renderCustomPhotoPreview();
    }

    function collectAutonomyConfig() {
      const current = state.autonomy?.config || {};
      return {
        ...current,
        enabled: $("autoEnabled").checked,
        mode: $("autoMode").value,
        accounts: [...$("autoAccounts").querySelectorAll("input:checked")].map(input => input.value),
        account_strategy: $("autoAccountStrategy").value,
        start_time: $("autoStart").value,
        end_time: $("autoEnd").value,
        max_per_day: Number($("autoMaxDaily").value || 3),
        interval_minutes: Number($("autoInterval").value || 180),
        cooldown_days: Number($("autoCooldown").value || 0),
        slot_jitter_minutes: Number($("autoJitter").value || 0),
        active_days: [...$("autoDays").querySelectorAll("input:checked")].map(input => Number(input.value)),
        selected_families: [...$("autoFamilies").querySelectorAll("input:checked")].map(input => input.value),
        account_photo_assignments: autoPhotoDraft,
      };
    }

    async function saveAutonomy() {
      if (!$("autoAccounts").querySelector("input:checked")) throw new Error("Selecciona al menos una cuenta.");
      const result = await api("/api/autonomy/config", { method: "POST", body: JSON.stringify({ config: collectAutonomyConfig() }) });
      state.autonomy = result.autonomy;
      resetAutoPhotoDraft();
      renderAutonomy();
      toast("Reglas automáticas guardadas");
    }

    async function generateAutonomyWeek() {
      await saveAutonomy();
      const hasFuture = (state.autonomy.queue || []).some(item => ["planned", "queued", "retry"].includes(item.status));
      const replace = hasFuture && window.confirm("¿Reemplazar las publicaciones futuras por un calendario nuevo?");
      const result = await api("/api/autonomy/generate", { method: "POST", body: JSON.stringify({ replace }) });
      state.autonomy = result.autonomy;
      renderAutonomy();
      toast(`${result.result.created} publicaciones agregadas al calendario`);
    }

    async function approveAutonomyWeek() {
      const result = await api("/api/autonomy/approve", { method: "POST", body: "{}" });
      state.autonomy = result.autonomy;
      renderAutonomy();
      toast(`${result.approved} publicaciones aprobadas`);
    }

    async function autonomyItemAction(id, action) {
      if (action === "retry") {
        const item = (state.autonomy?.queue || []).find(entry => entry.id === id);
        if (item && /Publish/i.test(item.detail || "") && /incierto|puede haberse creado|puede existir/i.test(item.detail || "")) {
          const go = confirm(
            "Este anuncio pudo haberse publicado antes de la interrupcion.\n\n" +
            "Revisa Marketplace primero: si el anuncio ya existe, reintentar creara un duplicado.\n\n" +
            "¿Reintentar de todas formas?"
          );
          if (!go) return;
        }
      }
      const payload = { id, action };
      if (action === "move") payload.scheduled_at = document.querySelector(`[data-move-value="${CSS.escape(id)}"]`)?.value;
      const result = await api("/api/autonomy/item", { method: "POST", body: JSON.stringify(payload) });
      state.autonomy = result.autonomy;
      renderAutonomy();
    }

    function renderAssistant() {
      const accounts = state.accounts.accounts || {};
      $("assistantAccount").innerHTML = Object.keys(accounts).map(key => `<option value="${key}">${accounts[key].display_name || key}</option>`).join("");
      $("assistantAccount").value = state.campaign.default_account || Object.keys(accounts)[0] || "";
      const preferredInterval = String(state.campaign.default_interval_minutes ?? 60);
      if ([...$("assistantInterval").options].some(option => option.value === preferredInterval)) {
        $("assistantInterval").value = preferredInterval;
      }
      const activeJob = (state.campaign.jobs || []).find(job => job.enabled !== false);
      const currentStyle = activeJob ? jobModeValue(activeJob) : "grouped";
      const styleControl = document.querySelector(`input[name="assistantStyle"][value="${currentStyle}"]`);
      if (styleControl) styleControl.checked = true;
      $("assistantAiEnabled").checked = state.campaign.defaults?.ai_descriptions !== false;
      renderAssistantAiStatus();
      const presets = state.assistantPresets || {};
      const currentFamilies = selectedFamilies();
      $("assistantFamilies").innerHTML = Object.keys(presets).map((key) => {
        const preset = presets[key];
        const selected = currentFamilies.includes(key);
        const image = preset.imageUrl
          ? `<img class="family-thumb" src="${preset.imageUrl}" alt="${preset.label}">`
          : `<span class="family-thumb" aria-hidden="true"></span>`;
        const variants = Number(preset.variantCount || 0);
        const price = preset.priceFrom ? `<span>Desde C$${preset.priceFrom}</span>` : "";
        return `<label class="family ${selected ? "selected" : ""}">
          ${image}
          <span>
            <strong>${preset.label}</strong>
            <span class="muted">${preset.description || ""}</span>
            <span class="family-meta"><span>${variants} variantes</span>${price}</span>
          </span>
          <input type="checkbox" value="${key}" ${selected ? "checked" : ""}>
        </label>`;
      }).join("");
      $("assistantFamilies").querySelectorAll("input").forEach(input => input.addEventListener("change", () => {
        $("assistantDescriptionPreview").classList.add("hidden");
        renderAssistantSummary();
        renderAssistantSelectionCount();
        $("assistantFamilies").querySelectorAll(".family").forEach(card => {
          const box = card.querySelector("input");
          card.classList.toggle("selected", box.checked);
        });
        refreshAssistantReview();
      }));
      renderAssistantSelectionCount();
      renderAssistantAccountStatus();
      renderAssistantSummary();
      refreshAssistantReview();
    }

    function selectedAssistantFamilyKeys() {
      return [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
    }

    async function refreshAssistantReview() {
      const families = selectedAssistantFamilyKeys();
      if (!families.length) {
        assistantReview = {};
        renderAssistantReview();
        return;
      }
      $("assistantReview").className = "review-empty";
      $("assistantReview").textContent = "Cargando fotos y precio...";
      try {
        const query = new URLSearchParams({ families: families.join(",") });
        const result = await api(`/api/assistant/review?${query.toString()}`);
        const next = {};
        result.families.forEach(item => {
          const existing = assistantReview[item.key];
          next[item.key] = existing && existing.changed
            ? existing
            : { ...item, images: [...(item.images || [])], changed: false };
        });
        assistantReview = next;
        renderAssistantReview();
      } catch (error) {
        $("assistantReview").className = "review-empty";
        $("assistantReview").textContent = "No pude cargar las fotos. Actualiza el panel e inténtalo otra vez.";
        toast(error.message);
      }
    }

    function renderAssistantReview() {
      const entries = Object.values(assistantReview);
      if (!entries.length) {
        $("assistantReview").className = "review-empty";
        $("assistantReview").textContent = "Selecciona un producto para revisar sus fotos y precio.";
        return;
      }
      $("assistantReview").className = "review-grid";
      $("assistantReview").innerHTML = entries.map(item => {
        const images = (item.images || []).map((image, index) => `<div class="review-photo">
          <a href="${escapeHtml(image.url)}" target="_blank" title="Ver foto completa"><img src="${escapeHtml(image.url)}" alt="${escapeHtml(item.label)} - foto ${index + 1}"></a>
          <span class="review-photo-name" title="${escapeHtml(image.name)}">${escapeHtml(image.name)}</span>
          <div class="review-photo-controls">
            <button class="icon-button" data-review-move="${escapeHtml(item.key)}" data-photo-index="${index}" data-delta="-1" title="Mover foto antes" aria-label="Mover foto antes" ${index === 0 ? "disabled" : ""}>↑</button>
            <button class="icon-button" data-review-move="${escapeHtml(item.key)}" data-photo-index="${index}" data-delta="1" title="Mover foto después" aria-label="Mover foto después" ${index === item.images.length - 1 ? "disabled" : ""}>↓</button>
            <button class="icon-button danger" data-review-remove="${escapeHtml(item.key)}" data-photo-index="${index}" title="Quitar foto" aria-label="Quitar foto">×</button>
          </div>
        </div>`).join("");
        const addPhoto = item.images.length < 10 ? `<label class="add-photo" title="Agregar fotos a esta campaña">+ Agregar fotos<input data-review-add="${escapeHtml(item.key)}" type="file" accept="image/jpeg,image/png,image/webp" multiple hidden></label>` : "";
        return `<article class="review-card">
          <div class="review-card-head">
            <div><h4>${escapeHtml(item.label)}</h4><span class="muted">Hasta 10 fotos por publicación</span></div>
            <div class="review-price"><label for="reviewPrice-${escapeHtml(item.key)}">Precio C$</label><input id="reviewPrice-${escapeHtml(item.key)}" data-review-price="${escapeHtml(item.key)}" type="number" min="1" step="1" value="${escapeHtml(item.price)}"></div>
          </div>
          <div class="review-tags">
            <div class="review-tags-head"><label for="reviewTags-${escapeHtml(item.key)}">Etiquetas del producto</label><button type="button" data-review-generate-tags="${escapeHtml(item.key)}">Generar con IA</button></div>
            <input id="reviewTags-${escapeHtml(item.key)}" data-review-tags="${escapeHtml(item.key)}" value="${escapeHtml((item.tags || []).join(', '))}" placeholder="Ej. ropa deportiva, gimnasio, entrenamiento">
            <small>Se agregaran al campo nativo "Etiquetas del producto" de Facebook Marketplace.</small>
          </div>
          <div class="review-photos">${images || '<span class="muted">Sin fotos disponibles.</span>'}${addPhoto}</div>
        </article>`;
      }).join("");
      document.querySelectorAll("[data-review-price]").forEach(input => input.addEventListener("input", event => {
        const item = assistantReview[event.target.dataset.reviewPrice];
        if (!item) return;
        item.price = event.target.value;
        item.changed = true;
        setDirty();
        renderAssistantSummary();
      }));
      document.querySelectorAll("[data-review-tags]").forEach(input => input.addEventListener("input", event => {
        const item = assistantReview[event.target.dataset.reviewTags];
        if (!item) return;
        item.tags = event.target.value.split(/[,;\\n]/).map(tag => tag.trim()).filter(Boolean).slice(0, 8);
        item.changed = true;
        setDirty();
      }));
      document.querySelectorAll("[data-review-remove]").forEach(button => button.addEventListener("click", () => {
        const item = assistantReview[button.dataset.reviewRemove];
        if (!item) return;
        item.images.splice(Number(button.dataset.photoIndex), 1);
        item.changed = true;
        setDirty();
        renderAssistantReview();
      }));
      document.querySelectorAll("[data-review-move]").forEach(button => button.addEventListener("click", () => {
        const item = assistantReview[button.dataset.reviewMove];
        const from = Number(button.dataset.photoIndex);
        const to = from + Number(button.dataset.delta);
        if (!item || to < 0 || to >= item.images.length) return;
        [item.images[from], item.images[to]] = [item.images[to], item.images[from]];
        item.changed = true;
        setDirty();
        renderAssistantReview();
      }));
      document.querySelectorAll("[data-review-add]").forEach(input => input.addEventListener("change", () => uploadReviewPhotos(input.dataset.reviewAdd, [...input.files])));
      document.querySelectorAll("[data-review-generate-tags]").forEach(button => button.addEventListener("click", () => generateReviewTags(button.dataset.reviewGenerateTags, button)));
    }

    async function generateReviewTags(familyKey, button) {
      const item = assistantReview[familyKey];
      if (!item) return;
      button.disabled = true;
      button.textContent = "Generando...";
      try {
        const result = await api("/api/assistant/product-tags", {
          method: "POST",
          body: JSON.stringify({ family: familyKey, enabled: $("assistantAiEnabled").checked, model: state.ai?.model || "gpt-5.6-luna" }),
        });
        item.tags = result.tags || [];
        item.changed = true;
        setDirty();
        renderAssistantReview();
        toast(String(result.source || "").startsWith("openai:") ? "Etiquetas generadas con IA" : "Etiquetas comerciales generadas");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Generar con IA";
      }
    }

    function dataUrlForFile(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("No pude leer la foto seleccionada."));
        reader.readAsDataURL(file);
      });
    }

    async function uploadReviewPhotos(familyKey, files) {
      const item = assistantReview[familyKey];
      if (!item || !files.length) return;
      const allowed = Math.max(0, 10 - item.images.length);
      const selected = files.slice(0, allowed);
      if (files.length > allowed) toast("Solo se pueden usar hasta 10 fotos.");
      try {
        for (const file of selected) {
          if (file.size > 8 * 1024 * 1024) throw new Error(`${file.name} pesa más de 8 MB.`);
          const result = await api("/api/assistant/upload-image", {
            method: "POST",
            body: JSON.stringify({ filename: file.name, data: await dataUrlForFile(file) }),
          });
          item.images.push(result.media);
        }
        item.changed = true;
        setDirty();
        renderAssistantReview();
        toast("Fotos agregadas para esta campaña");
      } catch (error) {
        toast(error.message);
      }
    }

    function collectAssistantListingOverrides() {
      const overrides = {};
      selectedAssistantFamilyKeys().forEach(key => {
        const item = assistantReview[key];
        if (!item) return;
        const price = Number(item.price || 0);
        if (!price || price <= 0) throw new Error(`Indica un precio válido para ${item.label}.`);
        if (!item.images.length) throw new Error(`Agrega al menos una foto para ${item.label}.`);
        const tags = (item.tags || []).map(tag => String(tag).trim()).filter(Boolean).slice(0, 8);
        if (!tags.length) throw new Error(`Agrega al menos una etiqueta para ${item.label}.`);
        overrides[key] = { price, image_ids: item.images.map(image => image.id), tags };
      });
      return overrides;
    }

    function renderAssistantAiStatus() {
      const enabled = $("assistantAiEnabled").checked;
      const ai = state.ai || {};
      if (!enabled) {
        $("assistantAiStatus").textContent = "Usará la descripción comercial local del bot.";
      } else if (ai.configured) {
        $("assistantAiStatus").textContent = `IA lista · ${ai.model}`;
      } else {
        $("assistantAiStatus").textContent = "Usará redacción local hasta configurar OPENAI_API_KEY.";
      }
      $("assistantConfigureAiBtn").textContent = ai.configured ? "Cambiar clave" : "Configurar IA";
    }

    function renderAssistantSelectionCount() {
      const count = document.querySelectorAll("#assistantFamilies input:checked").length;
      $("assistantSelectionCount").textContent = `${count} ${count === 1 ? "seleccionado" : "seleccionados"}`;
    }

    function renderAssistantAccountStatus() {
      const key = $("assistantAccount").value;
      const account = (state.accounts.accounts || {})[key] || {};
      const status = (state.statuses || {})[key] || { ok: false, message: "sesion cerrada" };
      const el = $("assistantAccountStatus");
      el.classList.toggle("ok", Boolean(status.ok));
      el.classList.toggle("bad", !status.ok);
      el.innerHTML = `<span class="dot"></span><div><strong>${account.display_name || key || "Sin cuenta"}</strong><span>${status.ok ? "Sesión lista para publicar" : "Abre la sesión de Facebook"}</span></div>${status.ok ? "" : '<button id="assistantOpenSessionBtn">Abrir sesión</button>'}`;
      $("assistantPublishBtn").disabled = !status.ok;
      $("assistantPublishBtn").title = status.ok ? "" : "Abre la sesión de Facebook antes de publicar";
      const openButton = $("assistantOpenSessionBtn");
      if (openButton) openButton.onclick = () => openSession(key);
    }

    function selectedFamilies() {
      const checked = [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
      if (checked.length) return checked;
      const jobs = state.campaign.jobs || [];
      const inferred = [];
      for (const job of jobs) {
        if (job.enabled === false) continue;
        const title = JSON.stringify(job).toLowerCase();
        if (title.includes("sin mangas")) inferred.push("compression_sleeveless");
        else if (title.includes("leggin")) inferred.push("leggins");
        else if (title.includes("enterizo")) inferred.push("enterizos");
        else if (title.includes("shorts")) inferred.push("shorts");
        else if (title.includes("durag")) inferred.push("durags");
        else if (title.includes("bolso")) inferred.push("bolsos");
        else if (title.includes("muñequera") || title.includes("munequera")) inferred.push("munequeras");
        else if (title.includes("strap")) inferred.push("straps");
        else if (title.includes("\"ts\"") || title.includes("manga corta")) inferred.push("compression_short");
      }
      return [...new Set(inferred)].slice(0, 2);
    }

    function styleText(style) {
      return {
        grouped: "una publicacion agrupada",
        individual: "una publicacion por talla/color",
        grouped_by_color: "una publicacion por color",
        grouped_by_size: "una publicacion por talla",
      }[style] || style;
    }

    function renderAssistantSummary() {
      const presets = state.assistantPresets || {};
      const families = [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
      const names = families.map(key => presets[key]?.label || key);
      const accountKey = $("assistantAccount").value || "sin cuenta";
      const account = state.accounts.accounts?.[accountKey]?.display_name || accountKey;
      const styleValue = document.querySelector('input[name="assistantStyle"]:checked')?.value || "grouped";
      const style = styleText(styleValue);
      const interval = Number($("assistantInterval").value || 0);
      const startDelay = Number($("assistantStartDelay").value || 0);
      const descriptionMode = $("assistantAiEnabled").checked
        ? (state.ai?.configured ? `descripción con IA ${state.ai.model}` : "redacción comercial local")
        : "redacción comercial local";
      $("assistantSummary").innerHTML = families.length
        ? `<p><b>${names.join(", ")}</b></p>
           <p>${style} · ${account} · ${startDelay ? `inicia en ${startDelay} min` : "inicia ahora"} · cada ${interval} min · ${descriptionMode}</p>`
        : `<p><b>Selecciona al menos un producto</b></p><p class="muted">El bot elegirá las fotos y preparará la descripción.</p>`;
    }

    function renderGeneralCampaign() {
      const accounts = state.accounts.accounts || {};
      $("defaultAccount").innerHTML = Object.keys(accounts).map(key => `<option value="${key}">${key} - ${accounts[key].display_name || ""}</option>`).join("");
      $("defaultAccount").value = state.campaign.default_account || Object.keys(accounts)[0] || "";
      $("defaultInterval").value = state.campaign.default_interval_minutes ?? 60;
      $("defaultCategory").value = state.campaign.defaults?.category || "Men's clothing & shoes";
      $("defaultCondition").value = state.campaign.defaults?.condition || "New";
    }

    function jobModeValue(job) {
      if (job.listing_mode === "grouped" && job.group_by === "color") return "grouped_by_color";
      if (job.listing_mode === "grouped" && job.group_by === "size") return "grouped_by_size";
      if (job.listing_mode) return job.listing_mode;
      return "existing";
    }

    function setJobMode(job, mode) {
      delete job.group_by;
      if (mode === "existing") {
        delete job.listing_mode;
        delete job.source_excel;
        return;
      }
      job.listing_mode = mode;
      if (mode === "grouped_by_color") {
        job.listing_mode = "grouped";
        job.group_by = "color";
      }
      if (mode === "grouped_by_size") {
        job.listing_mode = "grouped";
        job.group_by = "size";
      }
      job.source_excel = job.source_excel || job.excel || "ArticulosGenerados.xlsx";
      job.images_root = job.images_root || "imagenes_firupost";
    }

    function renderJobs() {
      const jobs = state.campaign.jobs || [];
      $("jobs").innerHTML = "";
      jobs.forEach((job, index) => {
        const el = document.createElement("div");
        el.className = "job" + (job.enabled === false ? " disabled" : "");
        el.innerHTML = `
          <div class="row">
            <div class="col-5"><label>Nombre</label><input data-field="name" value="${job.name || ""}"></div>
            <div class="col-2"><label>Activo</label><select data-field="enabled"><option value="true">Si</option><option value="false">No</option></select></div>
            <div class="col-3"><label>Modo</label><select data-field="mode">
              <option value="existing">Excel existente</option>
              <option value="individual">Individual</option>
              <option value="grouped">Agrupado</option>
              <option value="grouped_by_color">Agrupado por color</option>
              <option value="grouped_by_size">Agrupado por talla</option>
            </select></div>
            <div class="col-2"><label>Delay min</label><input data-field="delay_minutes" type="number" min="0" value="${job.delay_minutes ?? 0}"></div>
            <div class="col-3"><label>Cuenta</label><select data-field="account">${accountOptions(job.account || state.campaign.default_account)}</select></div>
            <div class="col-3"><label>Item interval min</label><input data-field="item_interval_minutes" type="number" min="0" value="${job.item_interval_minutes ?? ""}"></div>
            <div class="col-3"><label>Excel/source</label><input data-field="excel" value="${job.source_excel || job.excel || "ArticulosGenerados.xlsx"}"></div>
            <div class="col-3"><label>Imagenes</label><input data-field="images_root" value="${job.images_root || "imagenes_firupost"}"></div>
            <div class="col-3"><label>Row index</label><input data-field="row_index" type="number" min="0" value="${job.row_index ?? 0}"></div>
            <div class="col-3"><label>SKUs</label><input data-field="skus" value="${arrayText(job.skus)}" placeholder="TSBK-M,TSBK-L"></div>
            <div class="col-3"><label>Prefijos SKU</label><input data-field="sku_prefixes" value="${arrayText(job.sku_prefixes)}" placeholder="TS,CC"></div>
            <div class="col-3"><label>Titulo contiene</label><input data-field="title_contains" value="${arrayText(job.title_contains)}" placeholder="sin mangas"></div>
            <div class="col-3"><label>Excluir titulo</label><input data-field="exclude_title_contains" value="${arrayText(job.exclude_title_contains)}" placeholder="sin mangas"></div>
            <div class="col-12 toolbar">
              <button data-action="preview">Previsualizar plan</button>
              <button data-action="duplicate">Duplicar</button>
              <button data-action="delete" class="danger">Eliminar</button>
            </div>
          </div>`;
        $("jobs").appendChild(el);
        el.querySelector('[data-field="enabled"]').value = String(job.enabled !== false);
        el.querySelector('[data-field="mode"]').value = jobModeValue(job);
        el.querySelectorAll("[data-field]").forEach(input => {
          input.addEventListener("input", () => updateJobFromCard(index, el));
          input.addEventListener("change", () => updateJobFromCard(index, el));
        });
        el.querySelector('[data-action="delete"]').onclick = () => { jobs.splice(index, 1); setDirty(); renderJobs(); renderOverview(); };
        el.querySelector('[data-action="duplicate"]').onclick = () => { jobs.splice(index + 1, 0, JSON.parse(JSON.stringify(job))); setDirty(); renderJobs(); renderOverview(); };
        el.querySelector('[data-action="preview"]').onclick = () => previewSingleJob(index);
      });
    }

    function accountOptions(selected) {
      const accounts = state.accounts.accounts || {};
      return Object.keys(accounts).map(key => `<option value="${key}" ${key === selected ? "selected" : ""}>${key}</option>`).join("");
    }

    function arrayText(value) {
      if (!value) return "";
      if (Array.isArray(value)) return value.join(",");
      return String(value);
    }

    function parseList(value) {
      return String(value || "").split(",").map(x => x.trim()).filter(Boolean);
    }

    function updateJobFromCard(index, el) {
      const job = state.campaign.jobs[index];
      job.name = el.querySelector('[data-field="name"]').value;
      job.enabled = el.querySelector('[data-field="enabled"]').value === "true";
      setJobMode(job, el.querySelector('[data-field="mode"]').value);
      job.delay_minutes = Number(el.querySelector('[data-field="delay_minutes"]').value || 0);
      job.account = el.querySelector('[data-field="account"]').value || undefined;
      const itemInterval = el.querySelector('[data-field="item_interval_minutes"]').value;
      if (itemInterval === "") delete job.item_interval_minutes; else job.item_interval_minutes = Number(itemInterval);
      const excel = el.querySelector('[data-field="excel"]').value;
      if (job.listing_mode) job.source_excel = excel; else job.excel = excel;
      job.images_root = el.querySelector('[data-field="images_root"]').value;
      job.row_index = Number(el.querySelector('[data-field="row_index"]').value || 0);
      for (const key of ["skus", "sku_prefixes", "title_contains", "exclude_title_contains"]) {
        const list = parseList(el.querySelector(`[data-field="${key}"]`).value);
        if (list.length) job[key] = list; else delete job[key];
      }
      setDirty();
      renderOverview();
    }

    function renderAccounts() {
      const accounts = state.accounts.accounts || {};
      $("accountsList").innerHTML = "";
      Object.keys(accounts).forEach(key => {
        const account = accounts[key];
        const st = (state.statuses || {})[key] || {};
        const el = document.createElement("div");
        el.className = "job";
        el.innerHTML = `
          <div class="row">
            <div class="col-2"><label>ID</label><input data-field="key" value="${key}"></div>
            <div class="col-3"><label>Nombre visible</label><input data-field="display_name" value="${account.display_name || ""}"></div>
            <div class="col-3"><label>Debugger address</label><input data-field="debugger_address" value="${account.debugger_address || ""}"></div>
            <div class="col-4"><label>Chrome profile</label><input data-field="chrome_profile" value="${account.chrome_profile || ""}"></div>
            <div class="col-12 toolbar">
              <span class="pill ${st.ok ? "ok" : "bad"}"><span class="dot"></span>${st.message || "sin estado"}</span>
              <button data-action="open">Abrir sesion</button>
              <button data-action="delete" class="danger">Eliminar</button>
            </div>
          </div>`;
        $("accountsList").appendChild(el);
        el.querySelectorAll("[data-field]").forEach(input => input.addEventListener("change", () => {
          const newKey = el.querySelector('[data-field="key"]').value.trim();
          const updated = {
            display_name: el.querySelector('[data-field="display_name"]').value,
            debugger_address: el.querySelector('[data-field="debugger_address"]').value,
            chrome_profile: el.querySelector('[data-field="chrome_profile"]').value,
          };
          if (newKey && newKey !== key) {
            delete state.accounts.accounts[key];
            state.accounts.accounts[newKey] = updated;
          } else {
            state.accounts.accounts[key] = updated;
          }
          setDirty();
          renderGeneralCampaign();
          renderAccounts();
          renderOverview();
        }));
        el.querySelector('[data-action="delete"]').onclick = () => {
          delete state.accounts.accounts[key];
          setDirty();
          renderAccounts();
          renderGeneralCampaign();
          renderOverview();
        };
        el.querySelector('[data-action="open"]').onclick = () => openSession(key);
      });
    }

    function renderInventory() {
      const query = ($("inventorySearch").value || $("inventoryQuick").value || "").toLowerCase();
      const rows = state.inventory.filter(row => {
        if (!query) return true;
        return [row.sku, row.title, row.category].join(" ").toLowerCase().includes(query);
      });
      $("inventoryTable").innerHTML = rows.map(row => `
        <tr>
          <td>${row.index}</td>
          <td>${row.sku}</td>
          <td>${row.title}</td>
          <td>${row.price}</td>
          <td>${row.imageFolder}/${row.imageName}</td>
          <td><button data-use-row="${row.index}">Crear job</button></td>
        </tr>`).join("");
      $("inventoryTable").querySelectorAll("[data-use-row]").forEach(btn => {
        btn.onclick = () => {
          const rowIndex = Number(btn.dataset.useRow);
          const row = state.inventory.find(item => item.index === rowIndex);
          state.campaign.jobs.push({
            name: row.title,
            enabled: false,
            delay_minutes: 0,
            excel: "ArticulosGenerados.xlsx",
            images_root: "imagenes_firupost",
            row_index: row.index,
          });
          setDirty();
          showTab("campaign");
          renderJobs();
          renderOverview();
          toast("Job creado deshabilitado");
        };
      });
    }

    function renderProcess() {
      const process = state.process || {};
      $("processStatus").textContent = process.status || "idle";
      $("logs").textContent = (process.logs || []).join("\n");
      $("logs").scrollTop = $("logs").scrollHeight;
    }

    function renderAll() {
      renderAssistant();
      renderAutonomy();
      renderGeneralCampaign();
      renderOverview();
      renderJobs();
      renderAccounts();
      renderInventory();
      renderProcess();
    }

    async function loadState() {
      const [baseState, autonomyState] = await Promise.all([api("/api/state"), api("/api/autonomy")]);
      state = baseState;
      state.autonomy = autonomyState.autonomy;
      resetAutoPhotoDraft();
      renderAll();
      setDirty(false);
    }

    async function saveConfig() {
      collectGeneralCampaign();
      await api("/api/save", { method: "POST", body: JSON.stringify({ accounts: state.accounts, campaign: state.campaign }) });
      setDirty(false);
      toast("Configuracion guardada");
      await loadState();
    }

    async function buildAssistantCampaign(action = "save") {
      const families = [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
      if (!families.length) {
        toast("Selecciona al menos un grupo de productos");
        return false;
      }
      let listingOverrides;
      try {
        listingOverrides = collectAssistantListingOverrides();
      } catch (error) {
        toast(error.message);
        return false;
      }
      const payload = {
        account: $("assistantAccount").value,
        style: document.querySelector('input[name="assistantStyle"]:checked')?.value || "grouped",
        interval_minutes: Number($("assistantInterval").value || 0),
        start_delay_minutes: Number($("assistantStartDelay").value || 0),
        ai_descriptions: $("assistantAiEnabled").checked,
        ai_model: state.ai?.model || "gpt-5.6-luna",
        families,
        listing_overrides: listingOverrides,
      };
      const result = await api("/api/assistant/apply", { method: "POST", body: JSON.stringify(payload) });
      state.campaign = result.campaign;
      setDirty(false);
      renderAll();
      toast("Campana creada por el asistente");
      if (action === "plan") await startRun("plan");
      if (action === "dry") {
        $("runFast").value = "true";
        $("runMaxJobs").value = "1";
        await startRun("dry");
      }
      return true;
    }

    async function previewAssistantDescription() {
      const family = document.querySelector("#assistantFamilies input:checked")?.value;
      if (!family) {
        toast("Selecciona un producto primero");
        return;
      }
      const button = $("assistantPreviewDescriptionBtn");
      button.disabled = true;
      button.textContent = "Generando...";
      try {
        const result = await api("/api/assistant/description-preview", {
          method: "POST",
          body: JSON.stringify({
            family,
            enabled: $("assistantAiEnabled").checked,
            model: state.ai?.model || "gpt-5.6-luna",
          }),
        });
        const source = String(result.source || "").startsWith("openai:") ? "Generada con IA" : "Redacción local";
        $("assistantDescriptionPreview").textContent = `${source}\n\n${result.description}`;
        $("assistantDescriptionPreview").classList.remove("hidden");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Ver descripción de ejemplo";
      }
    }

    async function saveAiConfiguration() {
      const apiKey = $("aiKeyInput").value.trim();
      if (!apiKey) {
        toast("Introduce la clave de OpenAI");
        return;
      }
      const button = $("saveAiBtn");
      button.disabled = true;
      button.textContent = "Guardando...";
      try {
        const result = await api("/api/ai/configure", {
          method: "POST",
          body: JSON.stringify({ api_key: apiKey }),
        });
        state.ai = result.ai;
        $("aiKeyInput").value = "";
        $("aiDialog").classList.add("hidden");
        renderAssistantAiStatus();
        renderAssistantSummary();
        toast("IA configurada");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Guardar clave";
      }
    }

    function collectGeneralCampaign() {
      state.campaign.default_account = $("defaultAccount").value;
      state.campaign.default_interval_minutes = Number($("defaultInterval").value || 0);
      state.campaign.defaults = state.campaign.defaults || {};
      state.campaign.defaults.category = $("defaultCategory").value;
      state.campaign.defaults.condition = $("defaultCondition").value;
      state.campaign.defaults.meet_public = true;
      state.campaign.defaults.door_pickup = true;
      state.campaign.defaults.door_dropoff = true;
    }

    async function previewSingleJob(index) {
      collectGeneralCampaign();
      const job = JSON.parse(JSON.stringify(state.campaign.jobs[index]));
      job.enabled = true;
      const payload = await api("/api/preview-job", { method: "POST", body: JSON.stringify({ job, campaign: state.campaign }) });
      showTab("run");
      $("logs").textContent = payload.output;
      toast("Plan generado");
    }

    async function openSession(key) {
      await api("/api/open-session", { method: "POST", body: JSON.stringify({ account: key }) });
      toast("Sesion solicitada");
    }

    async function startRun(mode) {
      collectGeneralCampaign();
      await saveConfig();
      const runMode = mode || $("runMode").value;
      if (runMode === "publish" && $("publishConfirm").value !== "PUBLICAR") {
        toast("Escribe PUBLICAR para publicar");
        return;
      }
      const payload = {
        mode: runMode,
        fast: $("runFast").value === "true",
        maxJobs: Number($("runMaxJobs").value || 0),
      };
      await api("/api/run/start", { method: "POST", body: JSON.stringify(payload) });
      showTab("run");
      toast("Proceso iniciado");
      await pollProcess();
    }

    async function stopRun() {
      await api("/api/run/stop", { method: "POST", body: "{}" });
      await pollProcess();
    }

    async function pollProcess() {
      const [processPayload, activityPayload] = await Promise.all([
        api("/api/run/status"),
        api("/api/activity"),
      ]);
      state.process = processPayload.process;
      state.activity = activityPayload.activity;
      renderProcess();
      renderOverview();
    }

    function showTab(name) {
      document.querySelectorAll(".tab").forEach(tab => tab.classList.add("hidden"));
      document.querySelectorAll("nav button").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === name));
      $(name).classList.remove("hidden");
    }

    document.querySelectorAll("nav button").forEach(btn => btn.onclick = () => showTab(btn.dataset.tab));
    $("refreshBtn").onclick = loadState;
    $("saveBtn").onclick = saveConfig;
    $("autoSaveBtn").onclick = saveAutonomy;
    $("autoGenerateBtn").onclick = generateAutonomyWeek;
    $("autoApproveBtn").onclick = approveAutonomyWeek;
    $("autoEnabled").onchange = saveAutonomy;
    $("customPhotos").onchange = () => uploadCustomPhotos([...$("customPhotos").files]);
    $("customSaveBtn").onclick = () => saveCustomProduct().catch(error => toast(error.message));
    $("addJobBtn").onclick = () => {
      state.campaign.jobs.push({
        name: "Nuevo job",
        enabled: false,
        delay_minutes: 0,
        listing_mode: "grouped",
        source_excel: "ArticulosGenerados.xlsx",
        images_root: "imagenes_firupost",
        title_contains: [],
      });
      setDirty();
      renderJobs();
      renderOverview();
    };
    $("addAccountBtn").onclick = () => {
      let key = "cuenta" + (Object.keys(state.accounts.accounts || {}).length + 1);
      state.accounts.accounts[key] = { display_name: key, debugger_address: "127.0.0.1:9223", chrome_profile: "%LOCALAPPDATA%\\MarketplaceBot_" + key };
      setDirty();
      renderAccounts();
      renderGeneralCampaign();
      renderOverview();
    };
    ["assistantInterval", "assistantStartDelay"].forEach(id => $(id).addEventListener("change", renderAssistantSummary));
    $("assistantAccount").addEventListener("change", () => {
      renderAssistantSummary();
      renderAssistantAccountStatus();
    });
    document.querySelectorAll('input[name="assistantStyle"]').forEach(input => input.addEventListener("change", renderAssistantSummary));
    $("assistantAiEnabled").addEventListener("change", () => {
      $("assistantDescriptionPreview").classList.add("hidden");
      renderAssistantAiStatus();
      renderAssistantSummary();
    });
    $("assistantPreviewDescriptionBtn").onclick = previewAssistantDescription;
    $("assistantConfigureAiBtn").onclick = () => {
      $("aiKeyInput").value = "";
      $("aiDialog").classList.remove("hidden");
    };
    $("cancelAiBtn").onclick = () => $("aiDialog").classList.add("hidden");
    $("saveAiBtn").onclick = saveAiConfiguration;
    $("assistantBuildBtn").onclick = () => buildAssistantCampaign("save");
    $("assistantDryBtn").onclick = () => buildAssistantCampaign("dry");
    $("assistantPublishBtn").onclick = () => {
      const count = document.querySelectorAll("#assistantFamilies input:checked").length;
      if (!count) {
        toast("Selecciona al menos un grupo de productos");
        return;
      }
      const account = state.accounts.accounts?.[$("assistantAccount").value]?.display_name || $("assistantAccount").value;
      $("publishDialogText").textContent = `El bot publicará ${count} grupo${count === 1 ? "" : "s"} de productos en la cuenta ${account}.`;
      $("publishDialog").classList.remove("hidden");
    };
    $("cancelPublishBtn").onclick = () => $("publishDialog").classList.add("hidden");
    $("confirmPublishBtn").onclick = async () => {
      $("publishDialog").classList.add("hidden");
      const built = await buildAssistantCampaign("save");
      if (!built) return;
      $("publishConfirm").value = "PUBLICAR";
      await startRun("publish");
    };
    ["defaultAccount", "defaultInterval", "defaultCategory", "defaultCondition"].forEach(id => $(id).addEventListener("change", () => { collectGeneralCampaign(); setDirty(); renderOverview(); }));
    $("inventorySearch").addEventListener("input", renderInventory);
    $("inventoryQuick").addEventListener("change", renderInventory);
    $("openSessionQuick").onclick = () => openSession(activeAccountKey());
    $("clearActivityBtn").onclick = async () => {
      if (!window.confirm("¿Eliminar todo el historial de actividad del bot?")) return;
      await api("/api/activity/clear", { method: "POST", body: "{}" });
      state.activity = { items: [] };
      renderOverview();
      toast("Historial limpiado");
    };
    $("startRunBtn").onclick = () => startRun();
    $("stopRunBtn").onclick = stopRun;
    setInterval(async () => {
      try { await pollProcess(); } catch (e) {}
    }, 2500);
    setInterval(async () => {
      try {
        const payload = await api("/api/autonomy");
        state.autonomy = payload.autonomy;
        renderAutonomy();
      } catch (e) {}
    }, 10000);
    loadState().catch(error => toast(error.message));
