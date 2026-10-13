/** Clipboard write with a fallback for non-secure (http://host) contexts. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    /* clipboard API needs a secure context — fall back to execCommand */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
