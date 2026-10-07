(function () {
  'use strict';

  function closestWithin(node, selector, boundary) {
    let current = node && node.nodeType === Node.ELEMENT_NODE ? node : node ? node.parentElement : null;
    while (current && current !== boundary) {
      if (current.matches && current.matches(selector)) return current;
      current = current.parentElement;
    }
    return null;
  }

  function currentListItem(editor) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return null;
    const node = selection.anchorNode;
    if (!editor.contains(node)) return null;
    return closestWithin(node, 'li', editor);
  }

  function placeCaretAtEnd(element) {
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function indentListItem(editor) {
    const item = currentListItem(editor);
    if (!item) return false;
    let previous = item.previousElementSibling;
    while (previous && previous.tagName !== 'LI') {
      previous = previous.previousElementSibling;
    }
    if (!previous) return false;
    const parentList = item.parentElement;
    const listTag = parentList && parentList.tagName ? parentList.tagName.toLowerCase() : 'ul';
    let nestedList = Array.from(previous.children).find(function (child) {
      return child.tagName && child.tagName.toLowerCase() === listTag;
    });
    if (!nestedList) {
      nestedList = document.createElement(listTag);
      previous.appendChild(nestedList);
    }
    nestedList.appendChild(item);
    placeCaretAtEnd(item);
    return true;
  }

  function outdentListItem(editor) {
    const item = currentListItem(editor);
    if (!item) return false;
    const parentList = item.parentElement;
    const parentItem = parentList ? parentList.parentElement : null;
    if (!parentList || !parentItem || parentItem.tagName !== 'LI') return false;
    parentItem.parentElement.insertBefore(item, parentItem.nextSibling);
    const hasItems = Array.from(parentList.children).some(function (child) {
      return child.tagName === 'LI';
    });
    if (!hasItems) {
      parentList.remove();
    }
    placeCaretAtEnd(item);
    return true;
  }

  const editors = Array.from(document.querySelectorAll('.topic-editor[contenteditable="true"]'));
  const ranges = new WeakMap();
  const dialog = document.getElementById('topic-table-dialog');
  let dialogEditor = null;

  function editorRange(editor) {
    const selection = window.getSelection();
    if (!selection || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    return editor.contains(range.startContainer) && editor.contains(range.endContainer) ? range : null;
  }

  function restoreSelection(editor) {
    editor.focus({preventScroll: true});
    const selection = window.getSelection();
    const saved = ranges.get(editor);
    if (saved && editor.contains(saved.startContainer) && editor.contains(saved.endContainer)) {
      selection.removeAllRanges();
      selection.addRange(saved.cloneRange());
    } else if (!editorRange(editor)) {
      placeCaretAtEnd(editor);
    }
  }

  function selectedCell(editor) {
    const range = editorRange(editor) || ranges.get(editor);
    if (!range || !editor.contains(range.startContainer)) return null;
    return closestWithin(range.startContainer, 'th, td', editor);
  }

  function updateTableTools(editor) {
    const cell = selectedCell(editor);
    const table = cell && cell.closest('table');
    const hasMergedCells = table && (
      Array.from(table.querySelectorAll('th, td')).some(function (item) {
        return item.colSpan > 1 || item.rowSpan > 1;
      }) || Array.from(table.rows).some(function (row) {
        return row.cells.length !== table.rows[0].cells.length;
      })
    );
    editor.closest('.topic-editor-shell').querySelectorAll('.topic-table-tools button').forEach(function (button) {
      button.disabled = !table || (button.dataset.tableAction !== 'delete' && hasMergedCells);
    });
  }

  function rememberSelection(editor) {
    const range = editorRange(editor);
    if (range) ranges.set(editor, range.cloneRange());
    updateTableTools(editor);
  }

  document.addEventListener('selectionchange', function () {
    editors.forEach(function (editor) {
      if (editorRange(editor)) rememberSelection(editor);
    });
  });

  function focusCell(cell) {
    const range = document.createRange();
    range.selectNodeContents(cell);
    range.collapse(true);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function emptyCell(tag) {
    const cell = document.createElement(tag);
    if (tag === 'th') cell.setAttribute('scope', 'col');
    cell.appendChild(document.createElement('br'));
    return cell;
  }

  // Replacing via insertHTML keeps table changes in the browser's undo history.
  function replaceTable(editor, table, replacement, rowIndex, columnIndex) {
    const tableIndex = Array.from(editor.querySelectorAll('table')).indexOf(table);
    const range = document.createRange();
    range.selectNode(table);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand('insertHTML', false, replacement.outerHTML);
    const inserted = editor.querySelectorAll('table')[tableIndex];
    if (inserted && inserted.rows[rowIndex]) focusCell(inserted.rows[rowIndex].cells[columnIndex]);
    rememberSelection(editor);
  }

  function changeTable(editor, action) {
    const cell = selectedCell(editor);
    if (!cell) return;
    const table = cell.closest('table');
    const rowIndex = cell.parentElement.rowIndex;
    const columnIndex = cell.cellIndex;
    if (action === 'delete') {
      const range = document.createRange();
      range.selectNode(table);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand('delete', false);
      rememberSelection(editor);
      return;
    }
    const copy = table.cloneNode(true);
    if (action === 'row') {
      const newRow = document.createElement('tr');
      Array.from(copy.rows[rowIndex].cells).forEach(function () { newRow.appendChild(emptyCell('td')); });
      const currentRow = copy.rows[rowIndex];
      // A row below the header belongs in tbody, not thead.
      if (currentRow.parentElement.tagName === 'THEAD') {
        const body = copy.tBodies[0] || copy.createTBody();
        body.prepend(newRow);
      } else {
        currentRow.after(newRow);
      }
      replaceTable(editor, table, copy, rowIndex + 1, columnIndex);
    } else if (action === 'column') {
      Array.from(copy.rows).forEach(function (row) {
        const reference = row.cells[columnIndex];
        reference.after(emptyCell(reference.tagName.toLowerCase()));
      });
      replaceTable(editor, table, copy, rowIndex, columnIndex + 1);
    }
  }

  editors.forEach(function (editor) {
    const toolbar = editor.closest('.topic-editor-shell').querySelector('.topic-editor-toolbar');
    toolbar.querySelectorAll('button').forEach(function (button) {
      button.addEventListener('mousedown', function (event) {
        // Keep both the selection and page position when using the sticky toolbar.
        if (event.button === 0) event.preventDefault();
      });
      button.addEventListener('click', function () {
        restoreSelection(editor);
        const action = button.dataset.tableAction;
        if (action === 'insert' && dialog) {
          rememberSelection(editor);
          dialogEditor = editor;
          dialog.showModal();
          return;
        }
        if (action) {
          changeTable(editor, action);
          return;
        }
        const command = button.dataset.command;
        if (command === 'indent') indentListItem(editor);
        else if (command === 'outdent') outdentListItem(editor);
        else {
          let value = button.dataset.value || null;
          if (command === 'createLink') {
            rememberSelection(editor);
            value = window.prompt('URL einfügen');
            if (!value) return;
            restoreSelection(editor);
            if (!/^https?:\/\//i.test(value) && !/^mailto:/i.test(value)) value = 'https://' + value;
          }
          document.execCommand(command, false, value);
        }
        rememberSelection(editor);
      });
    });
    editor.addEventListener('input', function () { rememberSelection(editor); });
    editor.addEventListener('keydown', function (event) {
      if (event.key !== 'Tab' || event.ctrlKey || event.metaKey || event.altKey) return;
      const cell = selectedCell(editor);
      if (!cell) return;
      const table = cell.closest('table');
      const cells = Array.from(table.rows).flatMap(function (row) { return Array.from(row.cells); });
      const next = cells[cells.indexOf(cell) + (event.shiftKey ? -1 : 1)];
      if (next) {
        event.preventDefault();
        focusCell(next);
        rememberSelection(editor);
      } else if (!event.shiftKey) {
        event.preventDefault();
        if (!table.nextSibling) {
          const range = document.createRange();
          range.setStartAfter(table);
          range.collapse(true);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.execCommand('insertHTML', false, '<p><br></p>');
        }
        const range = document.createRange();
        range.setStartAfter(table);
        range.collapse(true);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        rememberSelection(editor);
      }
    });
  });

  if (dialog) {
    dialog.querySelector('[data-table-cancel]').addEventListener('click', function () { dialog.close(); });
    dialog.addEventListener('close', function () {
      if (dialogEditor) restoreSelection(dialogEditor);
    });
    dialog.querySelector('form').addEventListener('submit', function (event) {
      event.preventDefault();
      const form = event.currentTarget;
      if (!form.reportValidity() || !dialogEditor) return;
      const rowCount = Number(form.elements.rows.value);
      const columnCount = Number(form.elements.columns.value);
      if (!Number.isInteger(rowCount) || rowCount < 1 || rowCount > 20 ||
          !Number.isInteger(columnCount) || columnCount < 1 || columnCount > 10) return;
      const editor = dialogEditor;
      dialog.close();
      restoreSelection(editor);
      // Insert outside an existing table to avoid accidental nested tables.
      const cell = selectedCell(editor);
      if (cell) {
        const range = document.createRange();
        range.setStartAfter(cell.closest('table'));
        range.collapse(true);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
      const table = document.createElement('table');
      const body = table.createTBody();
      for (let row = 0; row < rowCount; row++) {
        const header = row === 0 && form.elements.header.checked;
        const section = header ? table.createTHead() : body;
        const tr = section.insertRow();
        for (let column = 0; column < columnCount; column++) tr.appendChild(emptyCell(header ? 'th' : 'td'));
      }
      const previousTables = new Set(editor.querySelectorAll('table'));
      document.execCommand('insertHTML', false, table.outerHTML + '<p><br></p>');
      const inserted = Array.from(editor.querySelectorAll('table')).find(function (item) { return !previousTables.has(item); });
      if (inserted) focusCell(inserted.rows[0].cells[0]);
      rememberSelection(editor);
    });
  }
})();
