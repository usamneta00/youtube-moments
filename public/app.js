const state = {
    timer: null,
    seen: new Set(),
    updates: [],
    fullText: "",
    pageCount: 0,
    running: false,
    voiceEnabled: false,
    playlistActive: false,
    currentAudio: null,
    firstLoad: true,
    intervalMs: 20000,
};

const el = {
    url: document.getElementById("live-url"),
    start: document.getElementById("start-btn"),
    voice: document.getElementById("voice-btn"),
    listenAll: document.getElementById("listen-all-btn"),
    copyAll: document.getElementById("copy-all-btn"),
    clear: document.getElementById("clear-btn"),
    updates: document.getElementById("updates"),
    count: document.getElementById("update-count"),
    pageCount: document.getElementById("page-count"),
    voiceState: document.getElementById("voice-state"),
    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),
    lastCheck: document.getElementById("last-check"),
    sourceLabel: document.getElementById("source-label"),
};

function setStatus(text, type = "idle") {
    el.statusText.textContent = text;
    el.statusDot.className = "h-2.5 w-2.5 rounded-full";
    if (type === "live") el.statusDot.classList.add("bg-green-600", "pulse-dot");
    else if (type === "error") el.statusDot.classList.add("bg-red-600");
    else if (type === "loading") el.statusDot.classList.add("bg-amber-500");
    else el.statusDot.classList.add("bg-zinc-400");
}

function normalizeText(text) {
    return (text || "").replace(/\s+/g, " ").trim();
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(timeout);
    }
}

async function getArabicSpeechText(update) {
    const fallback = normalizeText(`${update.title ? update.title + ". " : ""}${update.text}`);
    if (!fallback) return "";

    try {
        const response = await fetchWithTimeout("/api/arabic-speech-text", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: update.title || "", text: update.text || "" }),
        }, 8000);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Arabic conversion failed");
        return normalizeText(data.text || fallback);
    } catch (error) {
        console.warn("Arabic speech conversion failed, using original text.", error);
        return fallback;
    }
}

async function speakUpdate(update) {
    if (!state.voiceEnabled) return;

    try {
        const response = await fetchWithTimeout("/api/arabic-tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: update.title || "", text: update.text || "" }),
        }, 12000);
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || "Arabic TTS failed");
        }

        const audioBlob = await response.blob();
        const audioUrl = URL.createObjectURL(audioBlob);
        if (state.currentAudio) {
            state.currentAudio.pause();
            URL.revokeObjectURL(state.currentAudio.src);
        }

        state.currentAudio = new Audio(audioUrl);
        await new Promise((resolve, reject) => {
            let settled = false;
            const finish = () => {
                if (settled) return;
                settled = true;
                URL.revokeObjectURL(audioUrl);
                resolve();
            };
            state.currentAudio.onended = finish;
            state.currentAudio.onpause = finish;
            state.currentAudio.onerror = () => {
                if (settled) return;
                settled = true;
                URL.revokeObjectURL(audioUrl);
                reject(new Error("Audio playback failed"));
            };
            state.currentAudio.play().catch(reject);
        });
    } catch (error) {
        console.warn("High quality Arabic TTS failed, falling back to browser voice.", error);
        await speakUpdateWithBrowserVoice(update);
    }
}

async function speakUpdateWithBrowserVoice(update) {
    if (!("speechSynthesis" in window)) return;
    const text = await getArabicSpeechText(update);
    if (!text) return;

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    const voices = window.speechSynthesis.getVoices();
    const arabicVoice = voices.find(v => /^ar/i.test(v.lang));
    if (arabicVoice) utterance.voice = arabicVoice;
    utterance.lang = arabicVoice?.lang || "ar-SA";
    utterance.rate = 0.9;
    utterance.pitch = 1;
    await new Promise((resolve, reject) => {
        utterance.onend = resolve;
        utterance.onerror = event => {
            if (event.error === "interrupted" || event.error === "canceled") resolve();
            else reject(event);
        };
        window.speechSynthesis.speak(utterance);
    });
}

function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function orderedUpdatesOldestFirst() {
    return [...state.updates].sort((a, b) => {
        const byTime = String(a.time || "").localeCompare(String(b.time || ""));
        if (byTime !== 0) return byTime;
        return String(a.id || "").localeCompare(String(b.id || ""));
    });
}

function setListenAllButton(active) {
    if (!el.listenAll) return;
    if (active) {
        el.listenAll.className = "inline-flex h-11 items-center justify-center gap-2 rounded-md bg-accent px-4 text-sm font-extrabold text-white transition hover:bg-red-800";
        el.listenAll.innerHTML = '<i data-lucide="square" class="h-4 w-4"></i> إيقاف الاستماع';
    } else {
        el.listenAll.className = "inline-flex h-11 items-center justify-center gap-2 rounded-md border border-accent/25 bg-accent/5 px-4 text-sm font-extrabold text-accent transition hover:bg-accent hover:text-white";
        el.listenAll.innerHTML = '<i data-lucide="list-music" class="h-4 w-4"></i> استماع لكل التحديثات';
    }
    lucide.createIcons();
}

function stopPlaylist() {
    state.playlistActive = false;
    if (state.currentAudio) {
        state.currentAudio.pause();
        state.currentAudio = null;
    }
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    setListenAllButton(false);
}

async function playAllUpdates() {
    if (state.playlistActive) {
        stopPlaylist();
        return;
    }

    if (!state.updates.length) {
        setStatus("جاري جلب التحديثات للاستماع", "loading");
        await pollGuardian({ preserveStatus: true }).catch(() => null);
    }

    const updates = orderedUpdatesOldestFirst();
    if (!updates.length) {
        setStatus("لا توجد تحديثات للاستماع بعد. اضغط ابدأ المتابعة أو تأكد من الرابط", "error");
        return;
    }

    enableVoice();
    state.playlistActive = true;
    setListenAllButton(true);
    setStatus("جاري قراءة التحديثات من الأقدم للأحدث", "loading");

    try {
        for (let index = 0; index < updates.length; index += 1) {
            if (!state.playlistActive) break;
            setStatus(`قراءة ${index + 1} من ${updates.length}`, "loading");
            await speakUpdate(updates[index]);
            if (!state.playlistActive) break;
            const quietDelay = 1800 + Math.min(index % 4, 3) * 700;
            await wait(quietDelay);
        }
    } finally {
        state.playlistActive = false;
        setListenAllButton(false);
        setStatus(state.running ? "متصل ويتابع" : "جاهز", state.running ? "live" : "idle");
    }
}

function renderUpdates() {
    el.count.textContent = state.updates.length;
    el.pageCount.textContent = state.pageCount;
    if (!state.updates.length) {
        el.updates.innerHTML = `
            <div class="rounded-md border border-dashed border-black/20 p-8 text-center text-black/50">
                لا توجد تحديثات معروضة بعد.
            </div>`;
        return;
    }

    el.updates.innerHTML = state.updates.map((update, index) => `
        <article class="update-card rounded-md border border-black/10 bg-white p-4 shadow-sm">
            <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
                <span class="rounded bg-${index === 0 ? "accent" : "guard"} px-2.5 py-1 text-xs font-extrabold text-white">
                    ${index === 0 ? "الأحدث" : `#${index + 1}`}
                </span>
                <a class="text-left text-xs font-bold text-guard hover:underline" href="${update.url}" target="_blank" rel="noreferrer" dir="ltr">
                    ${update.time_label || formatTime(update.time) || "Guardian"}
                </a>
            </div>
            ${update.title ? `<h3 class="mb-2 text-lg font-extrabold leading-8">${escapeHtml(update.title)}</h3>` : ""}
            <p class="text-sm font-medium leading-8 text-black/75">${escapeHtml(update.text)}</p>
        </article>
    `).join("");
}

function buildFullTextFromUpdates() {
    const ordered = [...state.updates].sort((a, b) => String(a.time || "").localeCompare(String(b.time || "")));
    return ordered.map((update, index) => {
        const header = [
            `${index + 1}.`,
            update.time_label || "",
            update.title || "",
        ].filter(Boolean).join(" ");
        return `${header}\n${update.text || ""}`.trim();
    }).filter(Boolean).join("\n\n");
}

async function copyFullText() {
    const text = normalizeText(state.fullText) ? state.fullText : buildFullTextFromUpdates();
    if (!normalizeText(text)) {
        setStatus("لا يوجد نص لنسخه", "error");
        return;
    }

    try {
        await navigator.clipboard.writeText(text);
        setStatus("تم نسخ النص بالكامل", "live");
        el.copyAll.innerHTML = '<i data-lucide="check" class="h-4 w-4"></i> تم النسخ';
        lucide.createIcons();
        setTimeout(() => {
            el.copyAll.innerHTML = '<i data-lucide="copy" class="h-4 w-4"></i> نسخ النص بالكامل';
            lucide.createIcons();
        }, 1800);
    } catch (error) {
        setStatus("تعذر النسخ من المتصفح", "error");
        console.warn("Clipboard copy failed.", error);
    }
}

function escapeHtml(value) {
    return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("ar-SA", { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
}

async function pollGuardian(options = {}) {
    const { preserveStatus = false } = options;
    const url = el.url.value.trim();
    if (!url) return;

    if (!preserveStatus) setStatus("جاري الفحص", "loading");
    el.sourceLabel.textContent = url;

    try {
        const response = await fetch(`/api/guardian-live?url=${encodeURIComponent(url)}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "تعذر جلب التحديثات");

        const incoming = Array.isArray(data.updates) ? data.updates : [];
        const newUpdates = incoming.filter(update => !state.seen.has(update.id));
        state.fullText = data.full_text || "";
        state.pageCount = data.page_count || 0;
        el.pageCount.textContent = state.pageCount;

        incoming.forEach(update => state.seen.add(update.id));
        if (newUpdates.length) {
            state.updates = [...newUpdates, ...state.updates]
                .filter((update, index, arr) => arr.findIndex(item => item.id === update.id) === index)
                .slice(0, 50);
            renderUpdates();
            if (!state.firstLoad) speakUpdate(newUpdates[0]);
        } else if (!state.updates.length && incoming.length) {
            state.updates = incoming;
            renderUpdates();
        }

        state.firstLoad = false;
        if (!preserveStatus) setStatus("متصل ويتابع", "live");
        el.lastCheck.textContent = `آخر فحص: ${new Date(data.fetched_at || Date.now()).toLocaleTimeString("ar-SA")} - ${state.pageCount} صفحات`;
    } catch (error) {
        setStatus("خطأ في الجلب", "error");
        el.lastCheck.textContent = error.message;
        throw error;
    }
}

function startPolling() {
    if (state.timer) clearInterval(state.timer);
    state.running = true;
    state.firstLoad = true;
    state.seen.clear();
    state.updates = [];
    state.fullText = "";
    state.pageCount = 0;
    renderUpdates();
    pollGuardian();
    state.timer = setInterval(pollGuardian, state.intervalMs);
    el.start.innerHTML = '<i data-lucide="pause" class="h-4 w-4"></i> إيقاف المتابعة';
    lucide.createIcons();
}

function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    state.running = false;
    setStatus("متوقف", "idle");
    el.start.innerHTML = '<i data-lucide="play" class="h-4 w-4"></i> ابدأ المتابعة';
    lucide.createIcons();
}

function enableVoice() {
    state.voiceEnabled = true;
    el.voiceState.textContent = "نعم";
    el.voice.className = "inline-flex h-11 items-center justify-center gap-2 rounded-md bg-accent px-4 text-sm font-extrabold text-white transition hover:bg-red-800";
    el.voice.innerHTML = '<i data-lucide="volume-2" class="h-4 w-4"></i> الصوت مفعل';
    lucide.createIcons();
}

el.start.addEventListener("click", () => state.running ? stopPolling() : startPolling());
el.voice.addEventListener("click", enableVoice);
if (el.listenAll) el.listenAll.addEventListener("click", playAllUpdates);
el.copyAll.addEventListener("click", copyFullText);
el.clear.addEventListener("click", () => {
    stopPlaylist();
    state.seen.clear();
    state.updates = [];
    state.fullText = "";
    state.pageCount = 0;
    state.firstLoad = true;
    renderUpdates();
});

renderUpdates();
setStatus("جاهز", "idle");
lucide.createIcons();
