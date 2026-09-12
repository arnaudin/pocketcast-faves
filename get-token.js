/* Pocket Casts refresh-token grabber.
   Run once (and again roughly yearly) in the DevTools Console on a logged-in
   https://pocketcasts.com tab. Copies the refresh token to your clipboard and
   prints only its expiry — never the token value itself. */
(() => {
  const cookie = document.cookie.split(";").map(c => c.trim()).find(c => c.startsWith("auth="));
  if (!cookie) {
    console.log("%cNo `auth` cookie here.", "color:red;font-weight:bold",
                "Open https://pocketcasts.com, confirm you are logged in, then re-run.");
    return;
  }
  let session;
  try { session = JSON.parse(decodeURIComponent(cookie.slice(5))); }
  catch (e) { console.log("Could not parse the auth cookie:", e); return; }

  if (!session.refreshToken) {
    console.log("auth cookie has no refreshToken. Fields present:", Object.keys(session));
    return;
  }

  copy(session.refreshToken);

  const claims = (jwt) => {
    const b = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(b + "===".slice((b.length + 3) % 4)));
  };
  const exp = new Date(claims(session.refreshToken).exp * 1000);

  console.log("%cRefresh token copied to clipboard.", "color:green;font-weight:bold");
  console.log("Valid until:", exp.toLocaleString());
  console.log("Now run this in Terminal:\n" +
    '  security add-generic-password -U -s pcfaves-pocketcasts-refresh -a default -w "$(pbpaste)"');
})();
