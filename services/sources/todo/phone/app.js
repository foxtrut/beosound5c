'use strict';

// Huskeliste phone page. Talks only to the API it is served next to, and puts
// list text into the page with textContent — never as HTML — so an item can't
// inject markup. The page's CSP also forbids inline and third-party script.
(() => {
    const POLL_MS = 4000;
    const MAX_TEXT = 120;

    const form = document.getElementById('add-form');
    const input = document.getElementById('add-text');
    const list = document.getElementById('items');
    const empty = document.getElementById('empty');
    const clearDone = document.getElementById('clear-done');
    const status = document.getElementById('status');

    let version = null;
    let editingId = null;   // polling leaves the list alone while an item is being edited

    async function api(method, path, body) {
        const options = { method, cache: 'no-store', credentials: 'same-origin', headers: {} };
        if (body !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        let response;
        try {
            response = await fetch(path, options);
        } catch (e) {
            throw new Error('Ingen forbindelse til BeoSound');
        }
        let data = null;
        try { data = await response.json(); } catch (e) { /* no JSON body */ }
        if (!response.ok) {
            throw new Error(data && typeof data.error === 'string' ? data.error : `Fejl ${response.status}`);
        }
        return data;
    }

    function showStatus(message) {
        status.textContent = message || '';
        status.hidden = !message;
    }

    async function refresh(force = false) {
        if (editingId && !force) return;
        const data = await api('GET', 'api/items');
        showStatus('');
        if (!force && data.version === version) return;
        version = data.version;
        render(Array.isArray(data.items) ? data.items : []);
    }

    async function change(method, path, body) {
        try {
            await api(method, path, body);
            await refresh(true);
        } catch (e) {
            await refresh(true).catch(() => {});
            showStatus(e.message);
        }
    }

    function itemPath(id) {
        return `api/items/${encodeURIComponent(id)}`;
    }

    function render(items) {
        editingId = null;
        list.replaceChildren(...items.map(renderItem));
        empty.hidden = items.length > 0;
        const doneCount = items.filter(item => item.done).length;
        clearDone.hidden = doneCount === 0;
        clearDone.textContent = `Ryd afkrydsede (${doneCount})`;
    }

    function renderItem(item) {
        const li = document.createElement('li');
        li.className = item.done ? 'item done' : 'item';

        const check = document.createElement('button');
        check.type = 'button';
        check.className = 'check';
        check.setAttribute('role', 'checkbox');
        check.setAttribute('aria-checked', String(!!item.done));
        check.setAttribute('aria-label', item.text);
        check.addEventListener('click', () => change('PATCH', itemPath(item.id), { done: !item.done }));

        const text = document.createElement('button');
        text.type = 'button';
        text.className = 'text';
        text.textContent = item.text;
        text.title = 'Tryk for at rette';
        text.addEventListener('click', () => startEdit(text, item));

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'remove';
        remove.textContent = '×';
        remove.setAttribute('aria-label', `Slet ${item.text}`);
        remove.addEventListener('click', () => change('DELETE', itemPath(item.id)));

        li.append(check, text, remove);
        return li;
    }

    function startEdit(textButton, item) {
        editingId = item.id;
        const field = document.createElement('input');
        field.type = 'text';
        field.className = 'edit';
        field.maxLength = MAX_TEXT;
        field.value = item.text;
        field.enterKeyHint = 'done';
        field.setAttribute('aria-label', 'Ret punkt');
        textButton.replaceWith(field);
        field.focus();
        field.select();

        let finished = false;
        const finish = async (save) => {
            if (finished) return;
            finished = true;
            const value = field.value.trim();
            if (save && value && value !== item.text) {
                await change('PATCH', itemPath(item.id), { text: value });
            } else {
                await refresh(true).catch(e => showStatus(e.message));
            }
        };
        field.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true); }
            else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
        });
        field.addEventListener('blur', () => finish(true));
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const value = input.value.trim();
        if (!value) return;
        input.value = '';
        try {
            await api('POST', 'api/items', { text: value });
            await refresh(true);
        } catch (err) {
            input.value = value;
            showStatus(err.message);
        }
        input.focus();
    });

    clearDone.addEventListener('click', () => {
        if (confirm('Fjern alle afkrydsede punkter?')) change('POST', 'api/clear-done', {});
    });

    function poll() {
        if (document.hidden) return;
        refresh().catch(e => showStatus(e.message));
    }
    document.addEventListener('visibilitychange', poll);
    setInterval(poll, POLL_MS);
    poll();
})();
