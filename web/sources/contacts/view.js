/**
 * Contacts Source Preset (KONTAKTER in Danish) — iframe-based ArcList V2 contact list
 *
 * Shows the contacts from beo-source-contacts (port 8794) via softarc/contacts.html.
 */

const _contactsController = (() => {
    function sendToIframe(type, data) {
        if (!window.IframeMessenger) return false;
        return IframeMessenger.sendToRoute('menu/contacts', type, data);
    }

    return {
        get isActive() { return true; },

        updateMetadata() {
            sendToIframe('reload-data', {});
        },

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
window.SourcePresets.contacts = {
    controller: _contactsController,
    item: { title: 'CONTACTS', path: 'menu/contacts' },
    after: 'menu/playing',
    view: {
        title: 'CONTACTS',
        content: '<div id="contacts-container" style="width:100%;height:100%;"></div>'
    },

    onAdd() {},

    onMount() {
        const container = document.getElementById('contacts-container');
        if (!container || container.querySelector('iframe')) return;
        const iframe = document.createElement('iframe');
        iframe.id = 'preload-contacts';
        iframe.src = 'softarc/contacts.html';
        iframe.style.cssText = 'width:100%;height:100%;border:none;border-radius:8px;box-shadow:0 5px 15px rgba(0,0,0,0.3);';
        container.appendChild(iframe);
        if (window.IframeMessenger) {
            IframeMessenger.registerIframe('menu/contacts', 'preload-contacts');
        }
    },

    onRemove() {
        if (window.IframeMessenger) {
            IframeMessenger.unregisterIframe('menu/contacts');
        }
        const container = document.getElementById('contacts-container');
        if (container) container.innerHTML = '';
    },
};
