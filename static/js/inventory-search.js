(() => {
  const form = document.getElementById('inventorySearch');
  if (!form || !window.fetch || !window.AbortController) return;
  const input = form.elements.q;
  const results = document.getElementById('inventoryResults');
  const status = document.getElementById('inventorySearchStatus');
  let timer, controller, revision = 0;

  function cancel() {
    clearTimeout(timer);
    revision += 1;
    if (controller) controller.abort();
    results.setAttribute('aria-busy', 'false');
  }

  async function search(version) {
    const url = new URL(form.action, window.location.href);
    url.search = new URLSearchParams(new FormData(form)).toString();
    url.searchParams.delete('page');
    controller = new AbortController();
    results.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, {signal: controller.signal});
      if (!response.ok || response.redirected) throw new Error('Search unavailable');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.getElementById('inventoryResults');
      if (!next || next.dataset.owner !== results.dataset.owner) throw new Error('Inventory unavailable');
      // Invalidate on typing, before the next delayed request even starts.
      if (version !== revision) return;
      results.innerHTML = next.innerHTML;
      results.dataset.count = next.dataset.count;
      history.replaceState(null, '', url);
      status.textContent = next.dataset.count === '0'
        ? 'No items found. Try another search or clear the search box.'
        : `${next.dataset.count} items found.`;
    } catch (error) {
      if (version === revision && error.name !== 'AbortError') {
        status.textContent = 'Search failed. Previous results are still shown. Press Enter or Search to retry.';
      }
    } finally {
      if (version === revision) results.setAttribute('aria-busy', 'false');
    }
  }

  function schedule(event) {
    cancel();
    status.textContent = 'Searching…';
    if (event.isComposing) return;
    const version = revision;
    timer = setTimeout(() => search(version), 300);
  }
  input.addEventListener('input', schedule);
  input.addEventListener('compositionstart', cancel);
  input.addEventListener('compositionend', schedule);
  // Keep Enter and the Search button as ordinary GET navigation, also without JS.
  form.addEventListener('submit', cancel);
  window.addEventListener('pagehide', cancel);
})();
