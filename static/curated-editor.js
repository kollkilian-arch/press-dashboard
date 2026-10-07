/* Build only the row being edited, using its exact saved values. */
const curatedEditorLoads = new WeakMap();

async function rowEdit(id) {
  const row = document.getElementById('article-row-' + id);
  if (!row || !row.dataset.editUrl || row.querySelector('.em') || curatedEditorLoads.has(row)) return;
  const button = row.querySelector('[data-row-edit]');
  const controller = new AbortController();
  curatedEditorLoads.set(row, controller);
  const status = row.querySelector('[data-editor-status]') || document.createElement('span');
  status.dataset.editorStatus = '';
  status.className = 'small text-muted';
  status.setAttribute('role', 'status');
  status.textContent = 'Lädt…';
  button.parentElement.appendChild(status);
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');

  try {
    const response = await fetch(row.dataset.editUrl, {
      headers: { Accept: 'application/json' }, credentials: 'same-origin',
      cache: 'no-store', signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || 'Bearbeitungsfelder konnten nicht geladen werden.');
    if (curatedEditorLoads.get(row) !== controller) return;

    const fields = document.getElementById('curated-editor-fields');
    const actions = document.getElementById('curated-editor-actions');
    const form = actions.content.querySelector('form').cloneNode(true);
    form.id = 'edit-form-' + id;
    form.action = payload.save_url;
    form.querySelector('[data-row-cancel]').addEventListener('click', function () { rowCancel(id); });
    row.querySelectorAll('[data-edit-field]').forEach(function (cell) {
      const name = cell.dataset.editField;
      const input = fields.content.querySelector('[name="' + name + '"]').cloneNode(true);
      input.setAttribute('form', form.id);
      input.value = payload.fields[name] || '';
      cell.appendChild(input);
    });
    // The normal POST preserves the existing save, redirect and scroll behavior.
    button.closest('td').appendChild(form);
    row.querySelectorAll('.vm').forEach(function (el) { el.classList.add('d-none'); });
    status.remove();
  } catch (error) {
    if (curatedEditorLoads.get(row) !== controller) return;
    // Leave the row readable and retryable if the API or connection is unavailable.
    row.querySelectorAll('.em').forEach(function (el) { el.remove(); });
    if (error.name !== 'AbortError') {
      status.className = 'small text-danger';
      status.textContent = error.message || 'Bearbeitungsfelder konnten nicht geladen werden.';
    }
  } finally {
    if (curatedEditorLoads.get(row) === controller) {
      curatedEditorLoads.delete(row);
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }
}

function rowCancel(id) {
  const row = document.getElementById('article-row-' + id);
  if (!row) return;
  const controller = curatedEditorLoads.get(row);
  if (controller) controller.abort();
  curatedEditorLoads.delete(row);
  const button = row.querySelector('[data-row-edit]');
  if (button) {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
  row.querySelectorAll('.em, [data-editor-status]').forEach(function (el) { el.remove(); });
  row.querySelectorAll('.vm').forEach(function (el) { el.classList.remove('d-none'); });
}

function curatedImplicationsText(row) {
  if (!row) return '';
  return Array.from(row.querySelectorAll('.implications-text [data-text-line]'))
    .map(function (line) { return line.textContent; }).join('\n');
}
