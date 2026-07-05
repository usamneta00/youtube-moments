const runtimeAPI =
    (typeof chrome !== "undefined" && chrome.runtime) ||
    (typeof browser !== "undefined" && browser.runtime);

runtimeAPI.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action !== "fetchInsight") {
        return false;
    }

    const refresh = request.refresh ? "&refresh=true" : "";
    const url = `http://127.0.0.1:8000/api/video-insight-by-ytid/${request.ytId}?mode=${request.mode}${refresh}`;

    fetch(url)
        .then(res => res.json())
        .then(data => sendResponse({ success: true, data: data }))
        .catch(error => sendResponse({ success: false, error: error.message }));

    return true;
});
