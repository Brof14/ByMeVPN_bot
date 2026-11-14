(() => {
  const api = "/api/v1";
  let accessToken = "";
  const message = (text) => { document.getElementById("message").textContent = text; };
  async function jsonRequest(path, body) {
    const response = await fetch(api + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), credentials: "omit", cache: "no-store" });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "Request failed");
    return result;
  }
  function signedIn(result) {
    if (!result.access) throw new Error("Sign in was not completed");
    accessToken = result.access;
    document.getElementById("signin").hidden = true;
    document.getElementById("delete-section").hidden = false;
    document.getElementById("signed-in").textContent = `Signed in as ${result.user?.email || "your account"}.`;
    message("Review the confirmation before continuing.");
  }
  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); message("Signing in…");
    try { signedIn(await jsonRequest("/auth/login", { email: document.getElementById("email").value, password: document.getElementById("password").value })); }
    catch (error) { message(error.message); }
  });
  const checkbox = document.getElementById("confirm-loss");
  const button = document.getElementById("delete-button");
  checkbox.addEventListener("change", () => { button.disabled = !checkbox.checked || !accessToken; });
  button.addEventListener("click", async () => {
    if (!checkbox.checked || !accessToken) return;
    button.disabled = true; message("Submitting deletion request…");
    try {
      const response = await fetch(api + "/me", { method: "DELETE", headers: { Authorization: `Bearer ${accessToken}`, "X-Confirm-Account-Deletion": "DELETE" }, credentials: "omit", cache: "no-store" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) { message(result.detail || "Deletion was not confirmed. Check the result before retrying."); button.disabled = !checkbox.checked || !accessToken; return; }
      showDeleted();
    } catch (_) {
      message("The server response was lost. Checking whether deletion completed…");
      try {
        const status = await fetch(api + "/me/deletion-status", { headers: { Authorization: `Bearer ${accessToken}` }, credentials: "omit", cache: "no-store" });
        const result = await status.json().catch(() => ({}));
        if (status.ok && result.status === "deleted") { showDeleted(); return; }
        message("Deletion outcome could not be confirmed. Do not submit another request yet; reload this page and sign in again to check account access.");
      } catch (_) { message("Deletion outcome could not be confirmed. Do not submit another request yet; reload this page and sign in again to check account access."); }
      button.disabled = true;
    }
  });
  function showDeleted() {
    accessToken = "";
    document.getElementById("delete-section").hidden = true;
    message("Account deletion completed. Financial transaction records are retained where required for accounting and payment disputes.");
  }
  (async () => {
    try {
      const configResponse = await fetch(api + "/auth/google/web-client", { credentials: "omit", cache: "no-store" });
      if (!configResponse.ok) return;
      const { client_id: clientId } = await configResponse.json();
      const deadline = Date.now() + 10000;
      const poll = setInterval(() => {
        if (window.google?.accounts?.id) {
          clearInterval(poll);
          configureGoogle(clientId);
        } else if (Date.now() > deadline) { clearInterval(poll); }
      }, 100);
    } catch (_) { /* Email sign-in remains available when Google is not configured. */ }
  })();
  async function configureGoogle(clientId) {
    try {
      const { nonce } = await jsonRequest("/auth/google/nonce", {});
      google.accounts.id.initialize({ client_id: clientId, nonce, callback: async ({ credential }) => {
        message("Verifying Google sign-in…");
        try { signedIn(await jsonRequest("/auth/google", { id_token: credential, nonce })); }
        catch (error) { message(error.message); configureGoogle(clientId); }
      } });
      const target = document.getElementById("google-signin");
      target.replaceChildren();
      google.accounts.id.renderButton(target, { theme: "outline", size: "large", text: "signin_with" });
    } catch (_) { message("Google sign-in is unavailable. Email sign-in remains available."); }
  }
})();
