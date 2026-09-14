/**
 * Huskeliste Source Preset — iframe-based ArcList V2 list with tick-off
 *
 * Shows the to-do list from beo-source-todo (port 8793) via softarc/todo.html.
 * The service broadcasts "todo_update" whenever a phone changes the list, and
 * the controller passes that on so the arc reloads while it is on screen.
 */

const _todoController = (() => {
    function sendToIframe(type, data) {
        if (!window.IframeMessenger) return false;
        return IframeMessenger.sendToRoute('menu/todo', type, data);
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
window.SourcePresets.todo = {
    controller: _todoController,
    item: { title: 'HUSKELISTE', path: 'menu/todo' },
    after: 'menu/playing',
    view: {
        title: 'HUSKELISTE',
        content: '<div id="todo-container" style="width:100%;height:100%;"></div>'
    },

    onAdd() {},

    onMount() {
        const container = document.getElementById('todo-container');
        if (!container || container.querySelector('iframe')) return;
        const iframe = document.createElement('iframe');
        iframe.id = 'preload-todo';
        iframe.src = 'softarc/todo.html';
        iframe.style.cssText = 'width:100%;height:100%;border:none;border-radius:8px;box-shadow:0 5px 15px rgba(0,0,0,0.3);';
        container.appendChild(iframe);
        if (window.IframeMessenger) {
            IframeMessenger.registerIframe('menu/todo', 'preload-todo');
        }
    },

    onRemove() {
        if (window.IframeMessenger) {
            IframeMessenger.unregisterIframe('menu/todo');
        }
        const container = document.getElementById('todo-container');
        if (container) container.innerHTML = '';
    },
};
