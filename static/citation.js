(function () {
  const feedback = document.getElementById('citation-feedback');
  let feedbackTimer;

  function citationContent(button) {
    const title = (button.dataset.citationTitle || '').trim();
    const date = button.dataset.citationDate || 'o. D.';
    const source = (button.dataset.citationSource || '').trim() || 'Unbekannte Quelle';
    let url = '';
    try {
      const parsed = new URL(button.dataset.citationUrl);
      if (parsed.protocol === 'https:' || parsed.protocol === 'http:') url = parsed.href;
    } catch (error) {
      // Articles entered manually may have no original URL.
    }

    const citation = document.createElement('p');
    citation.appendChild(document.createTextNode(title + ', ' + date + ', '));
    const sourceNode = document.createElement(url ? 'a' : 'span');
    sourceNode.textContent = source;
    if (url) sourceNode.href = url;
    citation.appendChild(sourceNode);
    return {
      html: citation.outerHTML,
      text: citation.textContent + (url ? ' (' + url + ')' : ''),
    };
  }

  function copyWithEvent(content) {
    // Also supports browsers without the Clipboard API and local HTTP setups.
    const activeElement = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : [];
    const textarea = document.createElement('textarea');
    textarea.value = content.text;
    textarea.setAttribute('readonly', '');
    textarea.style.cssText = 'position:fixed;top:0;left:-9999px;';
    document.body.appendChild(textarea);
    let copied = false;
    function onCopy(event) {
      if (!event.clipboardData) return;
      event.clipboardData.setData('text/plain', content.text);
      event.clipboardData.setData('text/html', content.html);
      event.preventDefault();
      copied = true;
    }
    document.addEventListener('copy', onCopy);
    try {
      textarea.select();
      return document.execCommand('copy') && copied;
    } catch (error) {
      return false;
    } finally {
      document.removeEventListener('copy', onCopy);
      textarea.remove();
      activeElement?.focus({ preventScroll: true });
      if (selection) {
        selection.removeAllRanges();
        ranges.forEach(range => selection.addRange(range));
      }
    }
  }

  async function copyCitation(content) {
    if (navigator.clipboard?.write && window.ClipboardItem) {
      try {
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': new Blob([content.html], { type: 'text/html' }),
          'text/plain': new Blob([content.text], { type: 'text/plain' }),
        })]);
        return 'Zitat kopiert.';
      } catch (error) {
        // Retry via a copy event if rich clipboard access is unavailable.
      }
    }
    if (copyWithEvent(content)) return 'Zitat kopiert.';
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(content.text);
      return 'Zitat als Text kopiert.';
    }
    throw new Error('Clipboard unavailable');
  }

  function showFeedback(message, failed) {
    if (!feedback) return;
    window.clearTimeout(feedbackTimer);
    feedback.textContent = message;
    feedback.classList.remove('d-none', 'alert-success', 'alert-danger');
    feedback.classList.add(failed ? 'alert-danger' : 'alert-success');
    feedbackTimer = window.setTimeout(() => feedback.classList.add('d-none'), failed ? 8000 : 3000);
  }

  document.addEventListener('click', async function (event) {
    const button = event.target.closest('[data-citation]');
    if (!button || button.disabled) return;
    event.preventDefault();
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      showFeedback(await copyCitation(citationContent(button)), false);
    } catch (error) {
      showFeedback('Kopieren fehlgeschlagen. Bitte den Zugriff auf die Zwischenablage im Browser erlauben und erneut versuchen.', true);
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  });
})();
