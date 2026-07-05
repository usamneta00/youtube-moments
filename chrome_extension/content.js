let aiPanel = null;
let insightPollTimer = null;

function injectPanel() {
    if (document.getElementById('yt-intelligence-panel')) return;

    const secondary = document.querySelector('#secondary-inner') || document.querySelector('#secondary');
    if (!secondary) {
        setTimeout(injectPanel, 1000);
        return;
    }

    aiPanel = document.createElement('div');
    aiPanel.id = 'yt-intelligence-panel';
    aiPanel.innerHTML = `
        <div class="yt-intel-header">
            <h3><span class="yt-intel-icon"></span> AI Analysis</h3>
            <div class="yt-intel-tabs">
                <button id="yt-intel-btn-highlights" class="active">Highlights</button>
                <button id="yt-intel-btn-principles">Principles</button>
            </div>
        </div>
        <div id="yt-intel-content" class="yt-intel-content">
            <div class="yt-intel-empty">Choose an analysis mode</div>
        </div>
    `;

    secondary.insertBefore(aiPanel, secondary.firstChild);

    document.getElementById('yt-intel-btn-highlights').addEventListener('click', () => loadInsight('highlights'));
    document.getElementById('yt-intel-btn-principles').addEventListener('click', () => loadInsight('first_principles'));

    loadInsight('highlights');
}

function getYouTubeId() {
    const urlParams = new URLSearchParams(window.location.search);
    return urlParams.get('v');
}

function formatSeconds(s) {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}

function sendRuntimeMessage(payload) {
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        return new Promise((resolve, reject) => {
            chrome.runtime.sendMessage(payload, (res) => {
                if (chrome.runtime.lastError) {
                    reject(new Error(chrome.runtime.lastError.message));
                } else {
                    resolve(res);
                }
            });
        });
    }

    if (typeof browser !== "undefined" && browser.runtime && browser.runtime.sendMessage) {
        return browser.runtime.sendMessage(payload);
    }

    return Promise.reject(new Error("Extension runtime is not available. Reload the extension and refresh YouTube."));
}

function renderHighlights(contentDiv, highlights, responseData, mode) {
    contentDiv.innerHTML = highlights.map((h, i) => `
        <div class="yt-intel-item" data-seconds="${h.seconds || 0}">
            <div class="yt-intel-item-num">${i + 1}</div>
            <div class="yt-intel-item-text">
                <div class="yt-intel-item-title">${h.title || ''}</div>
                <div class="yt-intel-item-desc">${h.reason_ar || h.text || ''}</div>
                <div class="yt-intel-item-time">${h.start_time || formatSeconds(h.seconds || 0)}</div>
            </div>
        </div>
    `).join('');

    if (responseData.analyzing) {
        const progress = responseData.progress || {};
        const progressText = progress.total_parts
            ? `Processed ${progress.completed_parts}/${progress.total_parts} parts.`
            : 'Analysis is still running.';
        contentDiv.innerHTML += `<div class="yt-intel-cached">${progressText} Showing partial results.</div>`;
    } else if (responseData.cached) {
        contentDiv.innerHTML += '<div class="yt-intel-cached">Saved result.</div>';
    }

    document.querySelectorAll('.yt-intel-item').forEach(el => {
        el.addEventListener('click', () => {
            const seconds = parseInt(el.getAttribute('data-seconds'), 10);
            const videoElement = document.querySelector('video');
            if (videoElement) {
                videoElement.currentTime = seconds;
                videoElement.play();
            }
        });
    });
}

function loadInsight(mode) {
    const ytId = getYouTubeId();
    if (!ytId) return;

    if (insightPollTimer) {
        clearTimeout(insightPollTimer);
        insightPollTimer = null;
    }

    document.getElementById('yt-intel-btn-highlights').classList.toggle('active', mode === 'highlights');
    document.getElementById('yt-intel-btn-principles').classList.toggle('active', mode === 'first_principles');

    const contentDiv = document.getElementById('yt-intel-content');
    contentDiv.innerHTML = '<div class="yt-intel-loading"><div class="yt-intel-spinner"></div><div>Starting analysis...<br>Results will appear as soon as each transcript part is processed.</div></div>';

    const msgPayload = { action: "fetchInsight", ytId: ytId, mode: mode };

    const sendMessageWithRetry = async (payload, retries = 3, delay = 1000) => {
        for (let i = 0; i < retries; i++) {
            try {
                return await sendRuntimeMessage(payload);
            } catch (error) {
                console.warn(`[yt-intelligence] Attempt ${i + 1} failed:`, error.message);
                if (i === retries - 1) throw error;
                await new Promise(r => setTimeout(r, delay));
            }
        }
    };

    const requestInsight = (showErrors = true, refresh = false) => {
        sendMessageWithRetry({ ...msgPayload, refresh })
            .then((response) => {
                if (!response || !response.success || (response.data && response.data.error)) {
                    const errorMsg = response?.data?.error || response?.error || 'Could not connect to the local server. Make sure server.py is running.';
                    contentDiv.innerHTML = `<div class="yt-intel-error">Error: ${errorMsg}</div>`;
                    return;
                }

                const data = response.data;
                const highlights = data.highlights || [];

                if (highlights.length > 0) {
                    renderHighlights(contentDiv, highlights, data, mode);
                } else if (data.analyzing) {
                    const progress = data.progress || {};
                    const progressText = progress.total_parts
                        ? `Processed ${progress.completed_parts}/${progress.total_parts} parts.`
                        : 'Waiting for the first result.';
                    contentDiv.innerHTML = `<div class="yt-intel-loading"><div class="yt-intel-spinner"></div><div>Analyzing transcript...<br>${progressText}</div></div>`;
                } else {
                    contentDiv.innerHTML = '<div class="yt-intel-empty">No highlights found.</div>';
                }

                if (data.analyzing) {
                    insightPollTimer = setTimeout(() => requestInsight(false, false), 5000);
                }
            })
            .catch(err => {
                console.error('[yt-intelligence] Request failed:', err);
                if (showErrors) {
                    contentDiv.innerHTML = `<div class="yt-intel-error">Error: ${err.message}</div>`;
                } else {
                    insightPollTimer = setTimeout(() => requestInsight(false, false), 5000);
                }
            });
    };

    requestInsight(true, true);
}

let currentUrl = location.href;
setInterval(() => {
    if (location.href !== currentUrl) {
        currentUrl = location.href;
        if (location.pathname === '/watch') {
            if (insightPollTimer) {
                clearTimeout(insightPollTimer);
                insightPollTimer = null;
            }
            if (aiPanel) {
                aiPanel.remove();
                aiPanel = null;
            }
            injectPanel();
        }
    }
}, 1000);

if (location.pathname === '/watch') {
    injectPanel();
}
