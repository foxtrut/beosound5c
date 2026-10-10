'use strict';

// To-do phone page. Talks only to the API it is served next to, and puts
// list text into the page with textContent — never as HTML — so an item can't
// inject markup. The page's CSP also forbids inline and third-party script.
//
// The service stamps <html lang> with the device's language (or the phone's,
// when the device is set to "auto"); every string on the page comes from the
// table below, and API errors arrive as codes translated here.
(() => {
    const POLL_MS = 4000;
    const MAX_TEXT = 120;

    const STRINGS = {
        en: {
            title: 'To-do',
            newItem: 'New item',
            placeholder: 'Add item…',
            add: 'Add',
            empty: 'The list is empty.',
            clearDone: 'Clear ticked ({n})',
            confirmClear: 'Remove all ticked items?',
            tapToEdit: 'Tap to edit',
            editItem: 'Edit item',
            deleteItem: 'Delete {text}',
            offline: 'No connection to the BeoSound',
            generic: 'Something went wrong ({status})',
            errors: {
                missing: 'The text is empty',
                empty: 'The text is empty',
                too_long: `The text is too long (max ${MAX_TEXT} characters)`,
                full: 'The list is full',
                not_found: 'That item no longer exists',
                save_failed: 'Could not save the list',
            },
        },
        da: {
            title: 'Huskeliste',
            newItem: 'Nyt punkt',
            placeholder: 'Tilføj punkt…',
            add: 'Tilføj',
            empty: 'Listen er tom.',
            clearDone: 'Ryd afkrydsede ({n})',
            confirmClear: 'Fjern alle afkrydsede punkter?',
            tapToEdit: 'Tryk for at rette',
            editItem: 'Ret punkt',
            deleteItem: 'Slet {text}',
            offline: 'Ingen forbindelse til BeoSound',
            generic: 'Noget gik galt ({status})',
            errors: {
                missing: 'Teksten er tom',
                empty: 'Teksten er tom',
                too_long: `Teksten er for lang (højst ${MAX_TEXT} tegn)`,
                full: 'Listen er fuld',
                not_found: 'Punktet findes ikke længere',
                save_failed: 'Kunne ikke gemme listen',
            },
        },
    };
    const S = STRINGS[document.documentElement.lang] || STRINGS.en;

    function t(key, vars) {
        let text = S[key];
        for (const [name, value] of Object.entries(vars || {})) {
            text = text.replace(`{${name}}`, () => value);
        }
        return text;
    }

    document.title = S.title;
    document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = S[el.dataset.i18n]; });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = S[el.dataset.i18nPlaceholder];
    });

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
            throw new Error(t('offline'));
        }
        let data = null;
        try { data = await response.json(); } catch (e) { /* no JSON body */ }
        if (!response.ok) {
            const code = data && typeof data.error === 'string' ? data.error : '';
            throw new Error(S.errors[code] || t('generic', { status: response.status }));
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
        clearDone.textContent = t('clearDone', { n: doneCount });
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
        text.title = t('tapToEdit');
        text.addEventListener('click', () => startEdit(text, item));

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'remove';
        remove.textContent = '×';
        remove.setAttribute('aria-label', t('deleteItem', { text: item.text }));
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
        field.setAttribute('aria-label', t('editItem'));
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
        if (confirm(t('confirmClear'))) change('POST', 'api/clear-done', {});
    });

    function poll() {
        if (document.hidden) return;
        refresh().catch(e => showStatus(e.message));
    }
    document.addEventListener('visibilitychange', poll);
    setInterval(poll, POLL_MS);
    poll();
})();
