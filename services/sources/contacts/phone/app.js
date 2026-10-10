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
    const FIELDS = ['name', 'phone', 'email', 'address', 'birthday', 'note'];
    const UPLOAD_SIZE = 800;    // the service crops to 400 px; this just keeps uploads small

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
            birthday: 'Birthday',
            choosePhoto: 'Choose photo',
            removePhoto: 'Remove photo',
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
                invalid_birthday: 'The birthday is not a valid date',
                photo_invalid: 'The picture could not be read — try another one',
                photo_too_large: 'The picture is too large',
                not_image: 'That file is not a picture',
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
            birthday: 'Fødselsdag',
            choosePhoto: 'Vælg billede',
            removePhoto: 'Fjern billede',
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
                invalid_birthday: 'Fødselsdagen er ikke en gyldig dato',
                photo_invalid: 'Billedet kunne ikke læses — prøv et andet',
                photo_too_large: 'Billedet er for stort',
                not_image: 'Filen er ikke et billede',
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
    const photoPreview = document.getElementById('photo-preview');
    const photoInput = document.getElementById('photo-input');
    const photoRemove = document.getElementById('photo-remove');

    let version = null;
    let editing = null;     // null = list shown; {} = new contact; contact = editing it
    let pendingPhoto = null;    // Blob chosen in the editor, uploaded on save
    let dropPhoto = false;      // "Remove photo" pressed in the editor
    let previewUrl = null;

    async function api(method, path, body) {
        const options = { method, cache: 'no-store', credentials: 'same-origin', headers: {} };
        if (body instanceof Blob) {
            options.headers['Content-Type'] = body.type || 'image/jpeg';
            options.body = body;
        } else if (body !== undefined) {
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

    function initials(name) {
        return (name || '?').split(' ').filter(Boolean).slice(0, 2)
            .map(part => part[0].toUpperCase()).join('');
    }

    // A round picture, or the contact's initials when there is none.
    function avatar(contact, className, src) {
        const box = document.createElement('span');
        box.className = className;
        const url = src || (contact.photo
            ? `${contactPath(contact.id)}/photo?v=${encodeURIComponent(contact.photo)}` : null);
        if (url) {
            const img = document.createElement('img');
            img.alt = '';
            img.src = url;
            box.append(img);
        } else {
            box.textContent = initials(contact.name);
        }
        return box;
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

        const text = document.createElement('span');
        text.className = 'contact-text';
        const name = document.createElement('span');
        name.className = 'contact-name';
        name.textContent = contact.name;
        text.append(name);

        const detail = contact.phone || contact.email || contact.note;
        if (detail) {
            const sub = document.createElement('span');
            sub.className = 'contact-detail';
            sub.textContent = detail;
            text.append(sub);
        }
        button.append(avatar(contact, 'avatar'), text);
        button.addEventListener('click', () => openEditor(contact));
        li.append(button);
        return li;
    }

    function showPreview() {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = pendingPhoto ? URL.createObjectURL(pendingPhoto) : null;
        const shown = dropPhoto ? { ...editing, photo: '' } : editing;
        const name = editor.elements.name.value || editing.name;
        photoPreview.replaceChildren(...avatar({ ...shown, name }, 'avatar large', previewUrl).childNodes);
        photoRemove.hidden = !(pendingPhoto || (editing.photo && !dropPhoto));
    }

    // Shrink the picture on the phone before upload: camera pictures are
    // several MB. Where the browser can't decode it, the original goes up and
    // the service decides.
    async function shrink(file) {
        try {
            const bitmap = await createImageBitmap(file);
            const side = Math.min(bitmap.width, bitmap.height);
            const size = Math.min(side, UPLOAD_SIZE);
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = size;
            canvas.getContext('2d').drawImage(bitmap,
                (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, size, size);
            bitmap.close();
            return await new Promise((resolve, reject) => canvas.toBlob(
                blob => blob ? resolve(blob) : reject(new Error('toBlob')), 'image/jpeg', 0.9));
        } catch (e) {
            return file;
        }
    }

    photoInput.addEventListener('change', async () => {
        const file = photoInput.files && photoInput.files[0];
        photoInput.value = '';
        if (!file) return;
        pendingPhoto = await shrink(file);
        dropPhoto = false;
        showPreview();
    });

    photoRemove.addEventListener('click', () => {
        pendingPhoto = null;
        dropPhoto = true;
        showPreview();
    });

    function openEditor(contact) {
        editing = contact || {};
        pendingPhoto = null;
        dropPhoto = false;
        const isNew = !editing.id;
        editorTitle.textContent = isNew ? S.newContact : S.editContact;
        for (const field of FIELDS) editor.elements[field].value = editing[field] || '';
        editor.elements.birthday.max = new Date().toISOString().slice(0, 10);
        showPreview();
        deleteButton.hidden = isNew;
        listView.hidden = true;
        editor.hidden = false;
        showStatus('');
        if (isNew) editor.elements.name.focus();
        window.scrollTo(0, 0);
    }

    async function closeEditor() {
        editing = null;
        pendingPhoto = null;
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
        editor.hidden = true;
        listView.hidden = false;
        await refresh(true).catch(e => showStatus(e.message));
    }

    editor.addEventListener('submit', async (e) => {
        e.preventDefault();
        const body = {};
        for (const field of FIELDS) body[field] = editor.elements[field].value.trim();
        try {
            const saved = editing.id
                ? await api('PATCH', contactPath(editing.id), body)
                : await api('POST', 'api/contacts', body);
            // From here on a retry edits this contact instead of adding it twice.
            editing = saved.contact;
            if (pendingPhoto) {
                await api('PUT', `${contactPath(editing.id)}/photo`, pendingPhoto);
            } else if (dropPhoto && editing.photo) {
                await api('DELETE', `${contactPath(editing.id)}/photo`);
            }
            await closeEditor();
        } catch (err) {
            if (editing.id) {
                editorTitle.textContent = S.editContact;
                deleteButton.hidden = false;
            }
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
