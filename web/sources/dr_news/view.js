/**
 * DR News Source Preset — iframe-based ArcList V2 browser
 *
 * Displays DR (Danmarks Radio) articles grouped by feed section.
 * Uses softarc/dr_news.html with ArcList V2 page views for article text.
 */

const _drNewsController = (() => {
    function sendToIframe(type, data) {
        if (!window.IframeMessenger) return false;
        return IframeMessenger.sendToRoute('menu/dr_news', type, data);
    }

    return {
        get isActive() { return true; },

        updateMetadata() {},

        handleNavEvent(data) {
            return sendToIframe('nav', { data });
        },

        handleButton(button) {
            if (sendToIframe('button', { button })) return true;
            return false;
        },
    };
})();

window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.dr_news = {
    controller: _drNewsController,
    item: { title: 'DR NYHEDER', path: 'menu/dr_news' },
    after: 'menu/playing',
    view: {
        title: 'DR NYHEDER',
        content: '<div id="dr-news-container" style="width:100%;height:100%;"></div>'
    },

    onAdd() {},

    onMount() {
        const container = document.getElementById('dr-news-container');
        if (!container || container.querySelector('iframe')) return;
        const iframe = document.createElement('iframe');
        iframe.id = 'preload-dr-news';
        iframe.src = 'softarc/dr_news.html';
        iframe.style.cssText = 'width:100%;height:100%;border:none;border-radius:8px;box-shadow:0 5px 15px rgba(0,0,0,0.3);';
        container.appendChild(iframe);
        if (window.IframeMessenger) {
            IframeMessenger.registerIframe('menu/dr_news', 'preload-dr-news');
        }
    },

    onRemove() {
        if (window.IframeMessenger) {
            IframeMessenger.unregisterIframe('menu/dr_news');
        }
        const container = document.getElementById('dr-news-container');
        if (container) container.innerHTML = '';
    },
};
