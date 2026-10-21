(function () {
  const feedback = document.getElementById('citation-feedback');
  const dialog = document.getElementById('citation-modal');
  if (!dialog) return;
  const options = Array.from(dialog.querySelectorAll('[data-citation-format]'));
  let selectedArticle = null;
  let feedbackTimer;

  function citationContent(article, format) {
    const title = (article.citationTitle || '').trim();
    const date = article.citationDate || 'o. D.';
    const source = (article.citationSource || '').trim() || 'Unbekannte Quelle';
    let url = '';
    try {
      const parsed = new URL(article.citationUrl);
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
    const content = {
      html: citation.outerHTML,
      text: citation.textContent + (url ? ' (' + url + ')' : ''),
    };
    if (format === 'long' && article.summary) {
      const heading = article.citationAiGenerated === 'true' ? 'KI-Zusammenfassung:' : 'Zusammenfassung:';
      const headingNode = document.createElement('p');
      const strong = document.createElement('strong');
      strong.textContent = heading;
      headingNode.appendChild(strong);
      content.html += headingNode.outerHTML;
      article.summary.split(/\n\s*\n/).forEach(function (paragraph) {
        const node = document.createElement('p');
        paragraph.split('\n').forEach(function (line, index) {
          if (index) node.appendChild(document.createElement('br'));
          node.appendChild(document.createTextNode(line));
        });
        content.html += node.outerHTML;
      });
      content.text += '\n\n' + heading + '\n' + article.summary;
    }
    return content;
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

  dialog.addEventListener('show.bs.modal', function (event) {
    const trigger = event.relatedTarget;
    if (!trigger?.matches('[data-citation]')) return;
    selectedArticle = { ...trigger.dataset };
    selectedArticle.summary = (selectedArticle.citationSummary || '').replace(/\\r\\n|\\n/g, '\n').replace(/\r\n?/g, '\n').trim();
    dialog.querySelector('#citation-article-title').textContent = selectedArticle.citationTitle;
    dialog.querySelector('#citation-summary-status').textContent = selectedArticle.summary
      ? 'Die Langzitation enthält die gespeicherte Zusammenfassung.'
      : 'Keine Zusammenfassung vorhanden. Die Kurzzitation ist verfügbar.';
    options.forEach(function (button) {
      button.disabled = button.dataset.citationFormat === 'long' && !selectedArticle.summary;
    });
  });

  options.forEach(function (button) {
    button.addEventListener('click', async function () {
      if (!selectedArticle || button.disabled) return;
      const content = citationContent(selectedArticle, button.dataset.citationFormat);
      const disabledStates = options.map(option => option.disabled);
      options.forEach(option => { option.disabled = true; });
      button.setAttribute('aria-busy', 'true');
      try {
        showFeedback(await copyCitation(content), false);
        bootstrap.Modal.getInstance(dialog)?.hide();
      } catch (error) {
        showFeedback('Kopieren fehlgeschlagen. Bitte den Zugriff auf die Zwischenablage im Browser erlauben und erneut versuchen.', true);
      } finally {
        options.forEach((option, index) => { option.disabled = disabledStates[index]; });
        button.removeAttribute('aria-busy');
      }
    });
  });
})();
