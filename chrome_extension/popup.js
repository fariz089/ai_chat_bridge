// AI Chat Bridge — Chrome Extension
// Captures cookies + localStorage from ChatGPT & Grok, pushes to bridge server

const PLATFORMS = {
  chatgpt: {
    name: "ChatGPT",
    domains: ["chatgpt.com", ".chatgpt.com"],
    originUrl: "https://chatgpt.com",
    detectUrl: "chatgpt.com",
    requiredCookies: ["__Secure-next-auth.session-token"],
  },
  grok: {
    name: "Grok",
    domains: ["grok.com", ".grok.com", "x.com", ".x.com"],
    originUrl: "https://grok.com",
    detectUrl: "grok.com",
    requiredCookies: ["sso"],
  },
};

function log(msg, type = "") {
  const el = document.getElementById("log");
  const line = document.createElement("div");
  if (type) line.className = type;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function getServerUrl() {
  const host = document.getElementById("server-host").value.trim() || "http://127.0.0.1";
  const port = document.getElementById("server-port").value.trim() || "5098";
  return `${host}:${port}`;
}

// ── Cookie extraction ──────────────────────────────────────────────
async function getCookiesForDomains(domains) {
  const all = [];
  for (const domain of domains) {
    const cookies = await chrome.cookies.getAll({ domain });
    for (const c of cookies) {
      if (!all.find((x) => x.name === c.name && x.domain === c.domain)) {
        all.push(c);
      }
    }
  }
  return all;
}

function chromeCookieToPlaywright(c) {
  return {
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path || "/",
    expires: c.expirationDate || -1,
    httpOnly: c.httpOnly || false,
    secure: c.secure || false,
    sameSite: (c.sameSite || "None").charAt(0).toUpperCase() + (c.sameSite || "none").slice(1),
  };
}

// ── localStorage extraction ────────────────────────────────────────
async function getLocalStorage(tabId, origin) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const items = [];
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i);
          items.push({ name: key, value: localStorage.getItem(key) });
        }
        return items;
      },
    });
    if (results && results[0] && results[0].result) {
      return [{ origin, localStorage: results[0].result }];
    }
  } catch (e) {
    log(`localStorage skip: ${e.message}`, "info");
  }
  return [{ origin, localStorage: [] }];
}

// ── Tab detection ──────────────────────────────────────────────────
async function detectPlatforms() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeTab = tabs[0];
  const url = activeTab ? activeTab.url || "" : "";

  for (const [key, plat] of Object.entries(PLATFORMS)) {
    const statusEl = document.getElementById(`status-${key}`);
    const btnEl = document.getElementById(`btn-${key}`);
    const detected = url.includes(plat.detectUrl);

    if (detected) {
      // Also verify required cookies exist
      const cookies = await getCookiesForDomains(plat.domains);
      const names = cookies.map((c) => c.name);
      const hasRequired = plat.requiredCookies.every((r) =>
        names.some((n) => n === r || n.startsWith(r + "."))
      );

      if (hasRequired) {
        statusEl.textContent = "✓ Logged In";
        statusEl.className = "platform-status detected";
        btnEl.disabled = false;
      } else {
        statusEl.textContent = "Page Open (Login needed)";
        statusEl.className = "platform-status not-detected";
        btnEl.disabled = false; // allow attempt anyway
      }
    } else {
      statusEl.textContent = "Not Detected";
      statusEl.className = "platform-status not-detected";
      btnEl.disabled = true;
    }
  }
}

// ── Capture & Push ─────────────────────────────────────────────────
async function captureSession(platformKey) {
  const plat = PLATFORMS[platformKey];
  const label = document.getElementById(`label-${platformKey}`).value.trim();
  const btn = document.getElementById(`btn-${platformKey}`);
  btn.disabled = true;
  btn.textContent = "Capturing...";

  try {
    log(`Capturing ${plat.name} cookies...`, "info");
    const cookies = await getCookiesForDomains(plat.domains);
    if (!cookies.length) {
      throw new Error("No cookies found — are you logged in?");
    }

    const pwCookies = cookies.map(chromeCookieToPlaywright);

    // Get active tab for localStorage
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const origins = await getLocalStorage(tabs[0].id, plat.originUrl);

    const storageState = {
      cookies: pwCookies,
      origins: origins,
    };

    log(`Got ${pwCookies.length} cookies, pushing to server...`, "info");

    const serverUrl = getServerUrl();
    const resp = await fetch(`${serverUrl}/extension/push`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: platformKey,
        label: label || "default",
        storage_state: storageState,
      }),
    });

    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(`Server error ${resp.status}: ${err}`);
    }

    const data = await resp.json();
    log(`✓ ${plat.name} session saved: ${data.file || "OK"}`, "ok");
    btn.textContent = "✓ Captured!";
    // Refresh platform status badges
    detectPlatforms();
    setTimeout(() => {
      btn.textContent = `Capture ${plat.name} Session`;
      btn.disabled = false;
    }, 2000);
  } catch (e) {
    log(`✗ ${plat.name} error: ${e.message}`, "err");
    btn.textContent = `Capture ${plat.name} Session`;
    btn.disabled = false;
  }
}

// ── Event bindings ─────────────────────────────────────────────────
document.getElementById("btn-chatgpt").addEventListener("click", () => captureSession("chatgpt"));
document.getElementById("btn-grok").addEventListener("click", () => captureSession("grok"));

// Load saved server settings
chrome.storage.local.get(["bridgeHost", "bridgePort"], (data) => {
  if (data.bridgeHost) document.getElementById("server-host").value = data.bridgeHost;
  if (data.bridgePort) document.getElementById("server-port").value = data.bridgePort;
});

// Save settings on change
document.getElementById("server-host").addEventListener("change", (e) => {
  chrome.storage.local.set({ bridgeHost: e.target.value.trim() });
});
document.getElementById("server-port").addEventListener("change", (e) => {
  chrome.storage.local.set({ bridgePort: e.target.value.trim() });
});

// Init
detectPlatforms();
log("AI Chat Bridge extension ready.", "info");
