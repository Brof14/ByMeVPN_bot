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
      if (!response.ok) throw new Error(result.detail || "Deletion could not be confirmed. Your account remains available; contact support through the app.");
      accessToken = "";
      document.getElementById("delete-section").hidden = true;
      message("Account deletion completed. Financial transaction records are retained where required for accounting and payment disputes.");
    } catch (error) { message(error.message); button.disabled = !checkbox.checked || !accessToken; }
  });
  (async () => {
    try {
      const configResponse = await fetch(api + "/auth/google/web-client", { credentials: "omit", cache: "no-store" });
      if (!configResponse.ok) return;
      const { client_id: clientId } = await configResponse.json();
      const { nonce } = await jsonRequest("/auth/google/nonce", {});
      const deadline = Date.now() + 10000;
      const poll = setInterval(() => {
        if (window.google?.accounts?.id) {
          clearInterval(poll);
          google.accounts.id.initialize({ client_id: clientId, nonce, callback: async ({ credential }) => {
            message("Verifying Google sign-in…");
            try {
              signedIn(await jsonRequest("/auth/google", { id_token: credential, nonce }));
            } catch (error) { message(error.message); }
          } });
          google.accounts.id.renderButton(document.getElementById("google-signin"), { theme: "outline", size: "large", text: "signin_with" });
        } else if (Date.now() > deadline) { clearInterval(poll); }
      }, 100);
    } catch (_) { /* Email sign-in remains available when Google is not configured. */ }
  })();
})();
