/** Keep selection in the query string and the screen in the hash, as the shell expects. */
export function navigateTo(screen: string, params: Record<string, string>) {
  history.pushState(null, "", `${location.pathname}?${new URLSearchParams(params)}#/${screen}`);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}
