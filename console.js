(async function () {
  if (!window.Clerk?.session) {
    alert("Please make sure you are logged in on suno.com.");
    return;
  }

  // Remove existing scraper UI if present
  document.getElementById("suno-scraper-ui")?.remove();

  // Create clean floating filter modal
  const modal = document.createElement("div");
  modal.id = "suno-scraper-ui";
  modal.style.cssText = `
    position: fixed; top: 20px; right: 20px; z-index: 999999;
    background: #181b26; border: 1px solid #2b3044; border-radius: 10px;
    padding: 18px; width: 320px; color: #f0f2f8; font-family: sans-serif;
    box-shadow: 0 10px 25px rgba(0,0,0,0.7); font-size: 13px;
  `;
  modal.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <strong style="font-size:14px; color:#fff;">Suno Library Scraper</strong>
      <button id="suno-close-btn" style="background:none; border:none; color:#8d94aa; font-size:18px; cursor:pointer;">&times;</button>
    </div>
    <div style="display:flex; flex-direction:column; gap:8px; margin-bottom:14px;">
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
        <input type="checkbox" id="filter-dislikes" checked style="accent-color:#ff3b5c;"> Exclude Disliked
      </label>
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
        <input type="checkbox" id="filter-trash" checked style="accent-color:#ff3b5c;"> Exclude Trashed
      </label>
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
        <input type="checkbox" id="filter-stems" checked style="accent-color:#ff3b5c;"> Exclude Stems (Vocals/Instrumental cuts)
      </label>
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
        <input type="checkbox" id="filter-liked" style="accent-color:#ff3b5c;"> Liked Songs Only
      </label>
      <div style="margin-top:4px;">
        <label style="font-size:11px; color:#8d94aa;">Max Songs (Leave empty for All):</label>
        <input type="number" id="filter-max" placeholder="e.g. 500" style="width:100%; background:#0b0d12; border:1px solid #2b3044; border-radius:4px; color:#fff; padding:6px; margin-top:3px; outline:none;">
      </div>
    </div>
    <button id="suno-start-btn" style="width:100%; background:#ff3b5c; color:#fff; border:none; padding:9px; border-radius:6px; font-weight:600; cursor:pointer; font-size:13px;">
      Start Harvest
    </button>
    <div id="suno-progress" style="margin-top:10px; font-size:12px; color:#8d94aa; text-align:center; display:none;"></div>
  `;
  document.body.appendChild(modal);

  document.getElementById("suno-close-btn").onclick = () => modal.remove();

  document.getElementById("suno-start-btn").onclick = async function () {
    const excludeDislikes = document.getElementById("filter-dislikes").checked;
    const excludeTrash = document.getElementById("filter-trash").checked;
    const excludeStems = document.getElementById("filter-stems").checked;
    const likedOnly = document.getElementById("filter-liked").checked;
    const maxLimitVal = document.getElementById("filter-max").value.trim();
    const maxLimit = maxLimitVal ? parseInt(maxLimitVal, 10) : Infinity;

    this.disabled = true;
    this.style.opacity = "0.5";
    const prog = document.getElementById("suno-progress");
    prog.style.display = "block";
    prog.textContent = "Connecting to session...";

    const urls = [];
    const seen = new Set();
    let cursor = null;
    let hasMore = true;
    let page = 0;

    const filters = {};
    if (excludeDislikes) filters.disliked = "False";
    if (excludeTrash) filters.trashed = "False";
    if (likedOnly) filters.is_liked = "True";

    while (hasMore && urls.length < maxLimit) {
      page++;
      prog.textContent = `Harvested ${urls.length} songs (Page ${page})...`;

      let res = null;
      let data = null;
      let attempts = 0;
      const maxAttempts = 6;

      while (attempts < maxAttempts) {
        attempts++;
        let token = null;
        try {
          token = await window.Clerk.session.getToken();
        } catch (_) {}
        if (!token) break;

        res = await fetch("https://studio-api-prod.suno.com/api/feed/v3", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${token}`
          },
          body: JSON.stringify({
            cursor: cursor,
            limit: 50,
            filters: filters
          })
        });

        // Handle Suno 429 rate limit with automatic exponential backoff
        if (res.status === 429) {
          const waitTime = attempts * 1500;
          prog.textContent = `Rate limited on page ${page}. Waiting ${waitTime / 1000}s (attempt ${attempts}/${maxAttempts})...`;
          await new Promise(r => setTimeout(r, waitTime));
          continue;
        }

        if (!res.ok) break;

        data = await res.json();
        break;
      }

      if (!res || !res.ok || !data) {
        console.warn(`[Suno Scraper] Stopping at page ${page}: server returned ${res?.status || "error"}`);
        break;
      }

      const clips = data.clips || [];

      for (const c of clips) {
        if (!c?.id) continue;
        const uid = c.id.toLowerCase();
        if (seen.has(uid)) continue;

        if (excludeStems) {
          const isStem = c.task === "stem" || c.task === "stems" || c.metadata?.stem_from_id || c.entity_type === "stem";
          if (isStem) continue;
        }

        seen.add(uid);
        urls.push(`https://suno.com/song/${uid}`);
        if (urls.length >= maxLimit) break;
      }

      cursor = data.next_cursor;
      hasMore = Boolean(data.has_more && cursor && clips.length > 0);
      await new Promise(r => setTimeout(r, 250));
    }

    if (!urls.length) {
      prog.textContent = "No matching songs found.";
      this.disabled = false;
      this.style.opacity = "1";
      return;
    }

    const text = urls.join("\n") + "\n";
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      copy(text);
    }

    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "urls.txt";
    document.body.appendChild(a);
    a.click();
    a.remove();

    prog.innerHTML = `<span style="color:#10b981; font-weight:bold;">Done! ${urls.length} songs saved.</span>`;
    setTimeout(() => modal.remove(), 4000);
  };
})();
