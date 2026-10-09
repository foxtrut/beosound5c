'use strict';

// Contacts phone page. Talks only to the API it is served next to, and puts
// contact text into the page with textContent — never as HTML — so a contact
// can't inject markup. The page's CSP also forbids inline and third-party script.
//
// The service stamps <html lang> with the device's language (or the phone's,
// when the device is set to "auto"); every string on the page comes from the
// table below, and API errors arrive as codes translated here.
(() => {
    const POLL_MS = 5000;
    const FIELDS = ['name', 'phone', 'email', 'address', 'note'];

    const STRINGS = {
        en: {
            title: 'Contacts',
            newContact: 'New contact',
            editContact: 'Edit contact',
            empty: 'No contacts yet.',
            name: 'Name',
            phone: 'Phone',
            email: 'E-mail',
            address: 'Address',
            note: 'Note',
            save: 'Save',
            cancel: 'Cancel',
            delete: 'Delete contact',
            confirmDelete: 'Delete {name}?',
            offline: 'No connection to the BeoSound',
            generic: 'Something went wrong ({status})',
            errors: {
                name_missing: 'The contact needs a name',
                too_long: 'One of the fields is too long',
                invalid: 'One of the fields is invalid',
                full: 'There are too many contacts',
                not_found: 'That contact no longer exists',
                save_failed: 'Could not save the contacts',
            },
        },
        da: {
            title: 'Kontakter',
            newContact: 'Ny kontakt',
            editContact: 'Ret kontakt',
            empty: 'Ingen kontakter endnu.',
            name: 'Navn',
            phone: 'Telefon',
            email: 'E-mail',
            address: 'Adresse',
            note: 'Note',
            save: 'Gem',
            cancel: 'Annuller',
            delete: 'Slet kontakt',
            confirmDelete: 'Slet {name}?',
            offline: 'Ingen forbindelse til BeoSound',
            generic: 'Noget gik galt ({status})',
            errors: {
                name_missing: 'Kontakten skal have et navn',
                too_long: 'Et af felterne er for langt',
                invalid: 'Et af felterne er ugyldigt',
                full: 'Der er for mange kontakter',
                not_found: 'Kontakten findes ikke længere',
                save_failed: 'Kunne ikke gemme kontakterne',
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

    const listView = document.getElementById('list-view');
    const list = document.getElementById('contacts');
    const empty = document.getElementById('empty');
    const status = document.getElementById('status');
    const editor = document.getElementById('editor');
    const editorTitle = document.getElementById('editor-title');
    const deleteButton = document.getElementById('delete');

    let version = null;
    let editing = null;     // null = list shown; {} = new contact; contact = editing it

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
        if (editing && !force) return;
        const data = await api('GET', 'api/contacts');
        showStatus('');
        if (!force && data.version === version) return;
        version = data.version;
        render(Array.isArray(data.contacts) ? data.contacts : []);
    }

    function contactPath(id) {
        return `api/contacts/${encodeURIComponent(id)}`;
    }

    function render(contacts) {
        list.replaceChildren(...contacts.map(renderContact));
        empty.hidden = contacts.length > 0;
    }

    function renderContact(contact) {
        const li = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'contact';

        const name = document.createElement('span');
        name.className = 'contact-name';
        name.textContent = contact.name;
        button.append(name);

        const detail = contact.phone || contact.email || contact.note;
        if (detail) {
            const sub = document.createElement('span');
            sub.className = 'contact-detail';
            sub.textContent = detail;
            button.append(sub);
        }
        button.addEventListener('click', () => openEditor(contact));
        li.append(button);
        return li;
    }

    function openEditor(contact) {
        editing = contact || {};
        const isNew = !editing.id;
        editorTitle.textContent = isNew ? S.newContact : S.editContact;
        for (const field of FIELDS) editor.elements[field].value = editing[field] || '';
        deleteButton.hidden = isNew;
        listView.hidden = true;
        editor.hidden = false;
        showStatus('');
        if (isNew) editor.elements.name.focus();
        window.scrollTo(0, 0);
    }

    async function closeEditor() {
        editing = null;
        editor.hidden = true;
        listView.hidden = false;
        await refresh(true).catch(e => showStatus(e.message));
    }

    editor.addEventListener('submit', async (e) => {
        e.preventDefault();
        const body = {};
        for (const field of FIELDS) body[field] = editor.elements[field].value.trim();
        try {
            if (editing.id) await api('PATCH', contactPath(editing.id), body);
            else await api('POST', 'api/contacts', body);
            await closeEditor();
        } catch (err) {
            showStatus(err.message);
        }
    });

    document.getElementById('cancel').addEventListener('click', () => closeEditor());

    deleteButton.addEventListener('click', async () => {
        if (!editing?.id || !confirm(t('confirmDelete', { name: editing.name }))) return;
        try {
            await api('DELETE', contactPath(editing.id));
            await closeEditor();
        } catch (err) {
            showStatus(err.message);
        }
    });

    document.getElementById('new-contact').addEventListener('click', () => openEditor(null));

    function poll() {
        if (document.hidden) return;
        refresh().catch(e => showStatus(e.message));
    }
    document.addEventListener('visibilitychange', poll);
    setInterval(poll, POLL_MS);
    poll();
})();
