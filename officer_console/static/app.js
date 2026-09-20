const POLL_MS = 1500;
let currentCase = null;
let choice = "approve";

const el = (id) => document.getElementById(id);
const inr = (n) => "\u20b9" + Number(n || 0).toLocaleString("en-IN");

function setConn(live) {
  el("conn-dot").classList.toggle("live", live);
  el("conn-label").textContent = live ? "Live" : "Connecting\u2026";
}

async function poll() {
  try {
    const res = await fetch("/api/pending");
    setConn(true);
    const data = await res.json();
    if (data && (!currentCase || currentCase.case_id !== data.case_id)) {
      currentCase = data;
      render(data);
    } else if (!data && currentCase) {
      // Server cleared the case (e.g. reset) without us submitting — go idle.
      currentCase = null;
      showEmpty();
    }
  } catch (e) {
    setConn(false);
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

function showEmpty() {
  el("empty-state").classList.remove("hidden");
  el("case").classList.add("hidden");
  el("done-state").classList.add("hidden");
}

function recBadgeClass(rec) {
  if (rec === "recommend_approval") return "approve";
  if (rec === "recommend_partial") return "partial";
  if (rec === "recommend_field_verification") return "verify";
  return "reject";
}
function recLabel(rec) {
  return {
    recommend_approval: "Recommend approval",
    recommend_partial: "Recommend partial approval",
    recommend_field_verification: "Recommend field verification",
    recommend_rejection: "Recommend rejection",
  }[rec] || rec;
}

function render(data) {
  el("empty-state").classList.add("hidden");
  el("done-state").classList.add("hidden");
  el("case").classList.remove("hidden");

  const { claim, pack, evidence, norms, integrity, case_id } = data;

  el("case-id").textContent = case_id;
  el("claimant-name").textContent = claim.claimant_name || "Unnamed claimant";
  el("claimant-sub").textContent =
    `${claim.village || "\u2014"}, ${claim.panchayat || ""} \u00b7 ${claim.district || ""} \u00b7 ${claim.disaster_ref || ""}`;

  const badge = el("rec-badge");
  badge.textContent = recLabel(pack.recommendation);
  badge.className = "rec-badge " + recBadgeClass(pack.recommendation);

  // Claim panel
  el("claim-kv").innerHTML = `
    <dt>Category</dt><dd>${fmt(claim.damage_category)}</dd>
    <dt>Claimant ID</dt><dd>${fmt(claim.masked_id)}</dd>
    <dt>Phone</dt><dd>${fmt(claim.phone)}</dd>
    <dt>Language</dt><dd>${(claim.language || "en").toUpperCase()}</dd>
  `;
  el("claim-desc").textContent = claim.damage_description || "";
  const flags = el("claim-flags");
  flags.innerHTML = "";
  if (claim.instruction_like_content) {
    flags.innerHTML += `<span class="flag">\u26a0 Instruction-like content detected in claim text \u2014 treated as data, not followed</span>`;
  }

  // Evidence panel
  const evPill = el("evidence-pill");
  const consistency = evidence.overall_consistency || "indeterminate";
  evPill.textContent = consistency.replace("_", " ");
  evPill.className = "pill " + (consistency === "consistent" ? "good" : consistency === "partially_consistent" ? "warn" : "bad");
  const photos = evidence.photos || [];
  el("evidence-body").innerHTML = photos.length
    ? `<div class="photo-row">${photos.map((p) => `
        <div class="photo-finding">
          <span class="sev">${fmt(p.damage_type_observed)} \u2014 ${fmt(p.severity)}</span>
          <span class="consist">${fmt(p.consistency_with_claim).replace("_", " ")}</span>
        </div>`).join("")}</div>`
    : `<p class="hint">No photographs were attached to this claim.</p>`;

  // Integrity panel
  const dupRisk = integrity.duplicate_risk || "none";
  const iPill = el("integrity-pill");
  iPill.textContent = `score ${integrity.integrity_score ?? "\u2014"}`;
  iPill.className = "pill " + ((integrity.integrity_score ?? 100) >= 70 ? "good" : "bad");
  el("integrity-kv").innerHTML = `
    <dt>Duplicate risk</dt><dd>${fmt(dupRisk)}${(integrity.matched_claims || []).length ? " (" + integrity.matched_claims.join(", ") + ")" : ""}</dd>
    <dt>Notified zone</dt><dd>${integrity.zone_valid ? "\u2713 Valid" : "\u2717 Not in notified zone"}</dd>
  `;

  // Entitlements table
  const items = norms.items || [];
  let ceiling = 0;
  el("ent-tbody").innerHTML = items.map((it, i) => {
    const amt = Number(it.entitlement_amount || 0);
    if (it.eligible === "yes") ceiling += amt;
    const cite = it.citation || {};
    const hasCite = cite.doc_title && cite.quoted_clause;
    return `
      <tr>
        <td>${fmt(it.item)}</td>
        <td><span class="elig ${it.eligible}">${fmt(it.eligible)}</span></td>
        <td>${it.entitlement_amount != null ? inr(amt) : "\u2014"}</td>
        <td>
          ${hasCite
            ? `<button class="citation-btn" data-idx="${i}">View source</button>
               <div class="citation-detail" id="cite-${i}">
                 <em>${cite.doc_title}, p.${cite.page}</em><br/>\u201c${cite.quoted_clause}\u201d
               </div>`
            : `<span class="hint">no citation \u2014 indeterminate</span>`}
        </td>
      </tr>`;
  }).join("");
  el("ceiling-amount").textContent = inr(ceiling);

  document.querySelectorAll(".citation-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      el(`cite-${btn.dataset.idx}`).classList.toggle("open");
    });
  });

  // Recommendation + findings
  el("rec-line").innerHTML = `Recommended amount: <strong>${inr(pack.recommended_amount)}</strong> \u00b7 confidence ${pack.confidence ?? "\u2014"}`;
  el("findings-list").innerHTML = (pack.key_findings || [])
    .map((f) => {
      const [who, ...rest] = String(f).split(":");
      return rest.length ? `<li><b>${who}:</b>${rest.join(":")}</li>` : `<li>${f}</li>`;
    }).join("");
  el("open-qs").innerHTML = (pack.open_questions || [])
    .map((q) => `<div class="oq-item">${q}</div>`).join("");

  // Citizen messages
  el("msg-en").textContent = pack.citizen_message_en || "\u2014";
  el("msg-ml").textContent = pack.citizen_message_ml || "\u2014";

  // Decision panel reset
  choice = "approve";
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.choice === "approve"));
  el("modify-row").classList.add("hidden");
  el("modify-amount").value = ceiling || "";
  el("modify-amount").max = ceiling || undefined;
  el("comments").value = "";
  el("submit-error").classList.add("hidden");
  updateSubmitButton();
  currentCase._ceiling = ceiling;
}

function fmt(v) {
  return v === undefined || v === null || v === "" ? "\u2014" : String(v).replace(/_/g, " ");
}

function updateSubmitButton() {
  const btn = el("submit-btn");
  btn.className = "submit-btn" + (choice === "reject" ? " mode-reject" : choice === "modify" ? " mode-modify" : "");
  btn.textContent = { approve: "Approve claim", modify: "Approve modified amount", reject: "Reject claim" }[choice];
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    choice = tab.dataset.choice;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    el("modify-row").classList.toggle("hidden", choice !== "modify");
    updateSubmitButton();
  });
});

el("submit-btn").addEventListener("click", async () => {
  if (!currentCase) return;
  const errEl = el("submit-error");
  errEl.classList.add("hidden");

  const payload = {
    case_id: currentCase.case_id,
    decision: choice,
    comments: el("comments").value.trim(),
    officer_name: "Village Officer (demo)",
  };
  if (choice === "modify") {
    const amt = Number(el("modify-amount").value);
    if (!amt || amt < 0) {
      errEl.textContent = "Enter a valid amount.";
      errEl.classList.remove("hidden");
      return;
    }
    payload.modified_amount = amt;
  }

  const btn = el("submit-btn");
  btn.disabled = true;
  try {
    const res = await fetch("/api/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Server rejected the decision (${res.status})`);
    }
    el("case").classList.add("hidden");
    el("done-state").classList.remove("hidden");
    el("done-detail").textContent =
      choice === "reject"
        ? "The claim has been marked rejected. The citizen will be notified."
        : "The decision has been passed to the backstop for final validation and recorded.";
    currentCase = null;
  } catch (e) {
    errEl.textContent = e.message || "Could not submit the decision. Try again.";
    errEl.classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
});

poll();
