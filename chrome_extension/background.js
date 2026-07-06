const runtimeAPI =
    (typeof chrome !== "undefined" && chrome.runtime) ||
    (typeof browser !== "undefined" && browser.runtime);

runtimeAPI.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action !== "fetchInsight") {
        return false;
    }

    const refresh = request.refresh ? "&refresh=true" : "";
    const url = `https://youtube-moments-production.up.railway.app/api/video-insight-by-ytid/${request.ytId}?mode=${request.mode}${refresh}`;

    fetch(url)
        .then(async res => {
            if (!res.ok) {
                try {
                    const errData = await res.json();
                    throw new Error(errData.detail || `HTTP error! Status: ${res.status}`);
                } catch (e) {
                    throw new Error(e.message || `HTTP error! Status: ${res.status}`);
                }
            }
            return res.json();
        })
        .then(data => sendResponse({ success: true, data: data }))
        .catch(error => sendResponse({ success: false, error: error.message }));

    return true;
});
