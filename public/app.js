const state = {
    ytId: "",
    mode: "highlights",
    pollTimer: null,
};

const el = {
    input: document.getElementById("video-input"),
    highlights: document.getElementById("highlights-btn"),
    principles: document.getElementById("principles-btn"),
    refresh: document.getElementById("refresh-btn"),
    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),
    statusDetail: document.getElementById("status-detail"),
    results: document.getElementById("results"),
    resultTitle: document.getElementById("result-title"),
    youtubeLink: document.getElementById("youtube-link"),
};

function setStatus(text, detail = "", type = "idle") {
    el.statusText.textContent = text;
    el.statusDetail.textContent = detail;
    el.statusDot.className = "h-2.5 w-2.5 rounded-full";
    if (type === "loading") el.statusDot.classList.add("bg-amber-500");
    else if (type === "ok") el.statusDot.classList.add("bg-green-600");
    else if (type === "error") el.statusDot.classList.add("bg-red-600");
    else el.statusDot.classList.add("bg-zinc-400");
}

function extractYouTubeId(value) {
    const text = (value || "").trim();
    if (/^[a-zA-Z0-9_-]{11}$/.test(text)) return text;

    try {
        const url = new URL(text);
        if (url.hostname.includes("youtu.be")) {
            const id = url.pathname.split("/").filter(Boolean)[0];
            return /^[a-zA-Z0-9_-]{11}$/.test(id) ? id : "";
        }
        const id = url.searchParams.get("v");
        return /^[a-zA-Z0-9_-]{11}$/.test(id || "") ? id : "";
    } catch {
        const match = text.match(/(?:v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
        return match ? match[1] : "";
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

function formatSeconds(value) {
    const seconds = Math.max(0, Number.parseInt(value || 0, 10));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function renderEmpty(message) {
    el.results.innerHTML = `
        <div class="rounded-md border border-dashed border-black/20 p-8 text-center text-black/50">
            ${escapeHtml(message)}
        </div>`;
}

function renderHighlights(items, data) {
    if (!items.length) {
        renderEmpty(data.analyzing ? "جاري التحليل. ستظهر النتائج فور توفرها." : "لم يتم العثور على لحظات.");
        return;
    }

    el.results.innerHTML = items.map((item, index) => {
        const seconds = Number.parseInt(item.seconds || 0, 10);
        const href = state.ytId ? `https://www.youtube.com/watch?v=${state.ytId}&t=${seconds}s` : "#";
        return `
            <article class="rounded-md border border-black/10 bg-white p-4 shadow-sm">
                <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <span class="rounded bg-yt px-2.5 py-1 text-xs font-extrabold text-white">#${index + 1}</span>
                    <a class="text-xs font-bold text-yt hover:underline" href="${href}" target="_blank" rel="noreferrer" dir="ltr">
                        ${escapeHtml(item.start_time || formatSeconds(seconds))}
                    </a>
                </div>
                <h3 class="mb-2 text-lg font-extrabold leading-8">${escapeHtml(item.title || "لحظة")}</h3>
                <p class="text-sm font-medium leading-8 text-black/75">${escapeHtml(item.reason_ar || item.text || "")}</p>
            </article>`;
    }).join("");

    if (data.analyzing) {
        const progress = data.progress || {};
        const progressText = progress.total_parts
            ? `تم تحليل ${progress.completed_parts}/${progress.total_parts} أجزاء.`
            : "التحليل مستمر.";
        el.results.innerHTML += `<div class="rounded-md bg-amber-50 p-3 text-sm font-bold text-amber-900">${progressText}</div>`;
    }
}

async function loadInsight(mode, refresh = false) {
    const ytId = extractYouTubeId(el.input.value);
    if (!ytId) {
        setStatus("رابط غير صالح", "أدخل رابط YouTube صحيح أو معرف فيديو من 11 حرفاً.", "error");
        return;
    }

    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.ytId = ytId;
    state.mode = mode;

    const videoUrl = `https://www.youtube.com/watch?v=${ytId}`;
    el.youtubeLink.href = videoUrl;
    el.youtubeLink.textContent = videoUrl;
    el.youtubeLink.classList.remove("hidden");
    el.resultTitle.textContent = mode === "first_principles" ? "المبادئ الأولى" : "اللحظات";

    el.highlights.classList.toggle("bg-yt", mode === "highlights");
    el.highlights.classList.toggle("text-white", mode === "highlights");
    el.principles.classList.toggle("bg-yt", mode === "first_principles");
    el.principles.classList.toggle("text-white", mode === "first_principles");

    setStatus("جاري التحليل", "سيتم جلب transcript من DownSub ثم تحليل اللحظات عبر MiniMax-M3.", "loading");
    renderEmpty("جاري بدء التحليل...");

    try {
        const url = `/api/video-insight-by-ytid/${encodeURIComponent(ytId)}?mode=${encodeURIComponent(mode)}${refresh ? "&refresh=true" : ""}`;
        const response = await fetch(url);
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.detail || data.error || "تعذر جلب التحليل");

        const highlights = Array.isArray(data.highlights) ? data.highlights : [];
        renderHighlights(highlights, data);

        if (data.analyzing) {
            setStatus("التحليل مستمر", "سيتم تحديث النتائج تلقائياً.", "loading");
            state.pollTimer = setTimeout(() => loadInsight(mode, false), 5000);
        } else {
            setStatus(data.cached ? "نتائج محفوظة" : "اكتمل التحليل", `${highlights.length} نتيجة`, "ok");
        }
    } catch (error) {
        setStatus("خطأ", error.message, "error");
        renderEmpty(error.message);
    }
}

el.highlights.addEventListener("click", () => loadInsight("highlights", false));
el.principles.addEventListener("click", () => loadInsight("first_principles", false));
el.refresh.addEventListener("click", () => loadInsight(state.mode, true));

renderEmpty("أدخل رابط فيديو YouTube ثم اختر نوع التحليل.");
lucide.createIcons();
